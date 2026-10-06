"""Lease takeover truth table (Plan §10.1 #34).

The Step C races (#6, #42, #43, #48, #63) live in tests/test_step_c_races.py and the §3.3 response matrix
(#8) in tests/test_response_matrix.py; both drive the real Universal Sender.
"""

import os
import subprocess
import time
import unittest

from herdr_bartender.handlers import handle_agent_status_changed
from herdr_bartender.process import get_process_start_time, is_pid_alive, own_start_time
from tests.support import SandboxTestCase


class DispatchTests(SandboxTestCase):
    def _seed(self, sid, record):
        with self.cache_mgr as data:
            data["sessions"][sid] = record
            self.cache_mgr.save(data)

    def test_p34_lease_takeover_truth_table(self):
        """Plan §10.1 #34: all six lease-takeover rows, including the PID-reuse defense."""
        dead_pid = 999999
        while is_pid_alive(dead_pid):
            dead_pid += 1

        sid_unclaimed = self.sid("w1:pTTUnclaimed")
        sid_same = self.sid("w1:pTTSame")
        sid_dead = self.sid("w1:pTTDead")
        sid_active = self.sid("w1:pTTActive")
        sid_grace = self.sid("w1:pTTGrace")
        sid_hung = self.sid("w1:pTTHung")

        dummy_proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(dummy_proc.wait)
        self.addCleanup(dummy_proc.kill)
        live_pid = dummy_proc.pid
        live_st = get_process_start_time(live_pid)

        # Row 1: unclaimed -> CLAIM
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pTTUnclaimed", "workspace_id": "w1", "agent": "claude"},
            {}, bridge_url=self.mock_url,
        )
        with self.cache_mgr as data:
            self.assertEqual(data["sessions"][sid_unclaimed].get("delivery_status"), "delivered", "Row 1: Unclaimed must be claimed and delivered")

        # Row 2: same process -> CLAIM / PROCEED
        self._seed(sid_same, {
            "sending_pid": os.getpid(),
            "lease_deadline": time.time() + 1.0,
            "lease_token": f"{os.getpid()}:{own_start_time()}:token",
            "seq": 1, "delivered_seq": 1, "desired_state": "Working",
            "pane_id": "w1:pTTSame", "agent": "Herdr", "last_event_at": time.time(),
        })
        handle_agent_status_changed(
            {"agent_status": "done", "pane_id": "w1:pTTSame", "workspace_id": "w1", "agent": "claude"},
            {}, bridge_url=self.mock_url,
        )
        with self.cache_mgr as data:
            self.assertEqual(data["sessions"][sid_same].get("delivered_state"), "Done", "Row 2: Same PID must claim and update state")

        # Row 3: dead PID -> immediate takeover
        self._seed(sid_dead, {
            "sending_pid": dead_pid,
            "lease_deadline": time.time() + 10.0,
            "lease_token": f"{dead_pid}:dead_start:100",
            "seq": 1, "delivered_seq": 0, "desired_state": "Working",
            "pane_id": "w1:pTTDead", "agent": "Herdr", "last_event_at": time.time(),
        })
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pTTDead", "workspace_id": "w1", "agent": "claude"},
            {}, bridge_url=self.mock_url,
        )
        with self.cache_mgr as data:
            self.assertEqual(data["sessions"][sid_dead].get("delivery_status"), "delivered", "Row 3: Dead PID lease must be taken over immediately")

        # Row 3b: reused PID with mismatched start time -> immediate takeover
        sid_reused = self.sid("w1:pTTReused")
        self._seed(sid_reused, {
            "sending_pid": live_pid,
            "lease_deadline": time.time() + 10.0,
            "lease_token": f"{live_pid}:fake_reused_old_start_time:100",
            "seq": 1, "delivered_seq": 0, "desired_state": "Working",
            "pane_id": "w1:pTTReused", "agent": "Herdr", "last_event_at": time.time(),
        })
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pTTReused", "workspace_id": "w1", "agent": "claude"},
            {}, bridge_url=self.mock_url,
        )
        with self.cache_mgr as data:
            self.assertEqual(data["sessions"][sid_reused].get("delivery_status"), "delivered", "Row 3b: Reused PID with mismatched start time must be taken over immediately")

        # Row 4: active PID within lease -> DEFER
        self._seed(sid_active, {
            "sending_pid": live_pid,
            "lease_deadline": time.time() + 1.2,
            "lease_token": f"{live_pid}:{live_st}:foreign_token",
            "seq": 1, "delivered_seq": 0, "desired_state": "Working",
            "pane_id": "w1:pTTActive", "agent": "Herdr", "last_event_at": time.time(),
        })
        handle_agent_status_changed(
            {"agent_status": "done", "pane_id": "w1:pTTActive", "workspace_id": "w1", "agent": "claude"},
            {}, bridge_url=self.mock_url,
        )
        with self.cache_mgr as data:
            s_act = data["sessions"][sid_active]
            self.assertEqual(s_act.get("lease_token"), f"{live_pid}:{live_st}:foreign_token", "Row 4: Active lease must defer takeover")
            self.assertTrue((self.state_dir / "reconciler.pending").exists(), "Row 4: Deferral must touch reconciler.pending")

        # Row 5: active PID within grace window -> DEFER
        self._seed(sid_grace, {
            "sending_pid": live_pid,
            "lease_deadline": time.time() - 0.2,
            "lease_token": f"{live_pid}:{live_st}:grace_token",
            "seq": 1, "delivered_seq": 0, "desired_state": "Working",
            "pane_id": "w1:pTTGrace", "agent": "Herdr", "last_event_at": time.time(),
        })
        handle_agent_status_changed(
            {"agent_status": "done", "pane_id": "w1:pTTGrace", "workspace_id": "w1", "agent": "claude"},
            {}, bridge_url=self.mock_url,
        )
        with self.cache_mgr as data:
            s_grc = data["sessions"][sid_grace]
            self.assertEqual(s_grc.get("lease_token"), f"{live_pid}:{live_st}:grace_token", "Row 5: Grace window must defer takeover")

        # Row 6: active PID hung -> CLAIM
        self._seed(sid_hung, {
            "sending_pid": live_pid,
            "lease_deadline": time.time() - 1.0,
            "lease_token": f"{live_pid}:{live_st}:hung_token",
            "seq": 1, "delivered_seq": 0, "desired_state": "Working",
            "pane_id": "w1:pTTHung", "agent": "Herdr", "last_event_at": time.time(),
        })
        handle_agent_status_changed(
            {"agent_status": "done", "pane_id": "w1:pTTHung", "workspace_id": "w1", "agent": "claude"},
            {}, bridge_url=self.mock_url,
        )
        with self.cache_mgr as data:
            s_hng = data["sessions"][sid_hung]
            self.assertEqual(s_hng.get("delivery_status"), "delivered", "Row 6: Hung lease must be claimed and delivered")
            self.assertEqual(s_hng.get("delivered_state"), "Done")


if __name__ == "__main__":
    unittest.main()
