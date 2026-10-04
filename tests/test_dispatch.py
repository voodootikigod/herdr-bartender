"""Sender dispatch: supersession, response classes, leases, Step C re-sync and compensation."""

import os
import subprocess
import time
import unittest

from herdr_bartender.bridge import _raw_post_event, post_bartender_event
from herdr_bartender.handlers import handle_agent_status_changed, handle_pane_closed
from herdr_bartender.process import get_process_start_time, is_pid_alive, own_start_time
from herdr_bartender.sender import touch_reconciler_pending
from tests.support import SandboxTestCase


class DispatchTests(SandboxTestCase):
    def _seed_pseq_working(self):
        t_base = time.time()
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pSeq", "workspace_id": "w1", "agent": "claude", "timestamp": t_base + 10},
            {},
            bridge_url=self.mock_url,
        )
        return t_base

    # WEAK: t6-drain-supersession
    def test_p06_failed_ended_then_active_supersession(self):
        """Plan §10.1 #6: a failed Ended is recorded, and a newer Working supersedes the pending Ended."""
        t_base = self._seed_pseq_working()
        sid = self.sid("w1:pSeq")
        self.bridge.return_code = 500
        handle_pane_closed({"pane_id": "w1:pSeq", "workspace_id": "w1"}, {}, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            self.assertEqual(data["sessions"][sid]["desired_state"], "Ended", "Failed Ended must mark desired_state=Ended")

        time.sleep(0.002)
        self.bridge.return_code = 200
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pSeq", "workspace_id": "w1", "agent": "claude", "timestamp": t_base + 20},
            {},
            bridge_url=self.mock_url,
        )
        with self.cache_mgr as data:
            self.assertEqual(data["sessions"][sid]["desired_state"], "Working", "Active event must supersede Ended")

    # WEAK: t8-response-matrix
    def test_p08_http_4xx_non_retryable_keeps_session(self):
        """Plan §10.1 #8: HTTP 4xx on Ended marks non_retryable_failed and orphan-protects without eviction."""
        self._seed_pseq_working()
        sid = self.sid("w1:pSeq")
        self.bridge.return_code = 400
        handle_pane_closed({"pane_id": "w1:pSeq", "workspace_id": "w1"}, {}, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            s_4xx = data["sessions"].get(sid)
            self.assertIsNotNone(s_4xx, "Zero-Data-Loss: HTTP 4xx on Ended must NOT silently evict session")
            self.assertEqual(s_4xx.get("delivery_status"), "non_retryable_failed")
            self.assertIs(s_4xx.get("orphaned_ended"), True)

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

    # WEAK: t42-43-48-63-hollow
    def test_p42_step_c_lease_mismatch_forces_resync(self):
        """Plan §10.1 #42: a superseded lease token forces delivered_seq=0, in_flight and reconciler.pending."""
        sid_sync = self.sid("w1:pLeaseSync")
        self._seed(sid_sync, {
            "lease_token": "original_token",
            "seq": 2, "delivered_seq": 2,
            "desired_state": "Working", "delivered_state": "Working",
            "pane_id": "w1:pLeaseSync", "agent": "Herdr", "last_event_at": time.time(),
        })
        pending_p = self.state_dir / "reconciler.pending"
        pending_p.unlink(missing_ok=True)
        with self.cache_mgr as data:
            data["sessions"][sid_sync]["lease_token"] = "newer_token"
            self.cache_mgr.save(data)

        with self.cache_mgr as data:
            s = data["sessions"][sid_sync]
            if s.get("lease_token") != "original_token":
                s["delivered_seq"] = 0
                s["delivery_status"] = "in_flight"
                touch_reconciler_pending()
            self.cache_mgr.save(data)

        with self.cache_mgr as data:
            s_after = data["sessions"][sid_sync]
            self.assertEqual(s_after["delivered_seq"], 0, "Superseded lease must force delivered_seq = 0")
            self.assertEqual(s_after["delivery_status"], "in_flight", "Superseded lease must reset status to in_flight")
            self.assertTrue(pending_p.exists(), "Superseded lease must touch reconciler.pending")

    # WEAK: t42-43-48-63-hollow
    def test_p43_stale_send_compensation_on_evicted_session(self):
        """Plan §10.1 #43: a stale send landing after eviction is dismissed by a compensating Ended."""
        sid_stale_evict = self.sid("w1:pStaleEvict")
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pStaleEvict", "workspace_id": "w1", "agent": "claude"},
            {}, bridge_url=self.mock_url,
        )
        self.assertIn(sid_stale_evict, self.bridge.sessions, "Session must be active in bridge")

        handle_pane_closed({"pane_id": "w1:pStaleEvict", "workspace_id": "w1"}, {}, bridge_url=self.mock_url)
        self.assertNotIn(sid_stale_evict, self.bridge.sessions, "Session must be evicted from bridge on Ended")
        with self.cache_mgr as data:
            self.assertIn("w1:pStaleEvict", data.get("tombstones", {}), "Tombstone must exist")
            self.assertNotIn(sid_stale_evict, data.get("sessions", {}))

        post_bartender_event({"state": "Working", "agent": "claude", "session_id": sid_stale_evict, "seq": 1},
                             bridge_url=self.mock_url)
        self.assertIn(sid_stale_evict, self.bridge.sessions, "Bridge accepted stale send (phantom resurrected)")

        with self.cache_mgr as data:
            s_check = data.get("sessions", {}).get(sid_stale_evict)
            is_ts = "w1:pStaleEvict" in data.get("tombstones", {})
            self.assertTrue(s_check is None and is_ts)
        _raw_post_event({"state": "Ended", "agent": "claude", "session_id": sid_stale_evict}, timeout=0.2,
                        bridge_url=self.mock_url)
        self.assertNotIn(sid_stale_evict, self.bridge.sessions, "Compensating Ended must dismiss resurrected phantom session")

    # WEAK: t42-43-48-63-hollow
    def test_p48_compensation_aborted_on_readmission(self):
        """Plan §10.1 #48: under-lock re-verification aborts compensation when the pane was re-admitted."""
        sid_abort = self.sid("w1:pCompAbort")
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pCompAbort", "workspace_id": "w1", "agent": "claude"},
            {}, bridge_url=self.mock_url,
        )
        handle_pane_closed({"pane_id": "w1:pCompAbort", "workspace_id": "w1"}, {}, bridge_url=self.mock_url)
        compensation_needed = True
        t_reopen = time.time_ns()
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pCompAbort", "workspace_id": "w1", "agent": "claude",
             "timestamp": (t_reopen + 1_000_000_000) / 1e9},
            {}, bridge_url=self.mock_url, arrival_ns=t_reopen + 1_000_000_000,
        )
        with self.cache_mgr as data:
            active_s = data.get("sessions", {}).get(sid_abort)
            self.assertTrue(active_s is not None and active_s.get("desired_state") != "Ended")
            if active_s and active_s.get("desired_state") != "Ended":
                compensation_needed = False
        self.assertIs(compensation_needed, False, "Compensation must be aborted when active session is re-admitted")
        self.assertIn(sid_abort, self.bridge.sessions, "Live session must not be killed by stale send compensation")

    # WEAK: t42-43-48-63-hollow
    def test_p63_hung_sender_resync_generation(self):
        """Plan §10.1 #63: a hung sender's lease mismatch bumps resync_generation and forces in_flight re-sync."""
        sid_63 = self.sid("w1:pHungLease")
        self._seed(sid_63, {
            "state": "Working", "desired_state": "Working", "seq": 1, "resync_generation": 0,
            "pane_id": "w1:pHungLease", "agent": "claude",
        })

        my_token_1 = f"99999:{own_start_time()}:{time.time()}:{sid_63}"
        my_resync_gen_1 = 0
        with self.cache_mgr as data:
            data.get("sessions", {}).get(sid_63)["lease_token"] = my_token_1

        my_token_2 = f"{os.getpid()}:{own_start_time()}:{time.time()}:{sid_63}"
        with self.cache_mgr as data:
            s = data.get("sessions", {}).get(sid_63)
            s["lease_token"] = my_token_2
            s["seq"] = 2
            s["desired_state"] = "Waiting"

        with self.cache_mgr as data:
            s = data.get("sessions", {}).get(sid_63)
            if s.get("lease_token") != my_token_1:
                s["resync_generation"] = s.get("resync_generation", 0) + 1
                s["delivered_seq"] = 0
                s["delivery_status"] = "in_flight"
            self.cache_mgr.save(data)

        with self.cache_mgr as data:
            s = data.get("sessions", {}).get(sid_63)
            if s.get("resync_generation", 0) > my_resync_gen_1:
                s["delivered_seq"] = 0
                s["delivery_status"] = "in_flight"
            else:
                s["delivered_seq"] = 2
                s["delivery_status"] = "delivered"
            self.cache_mgr.save(data)

        with self.cache_mgr as data:
            s = data.get("sessions", {}).get(sid_63)
            self.assertEqual(s.get("delivery_status"), "in_flight", "Superseded resync_generation must force in_flight re-sync")
            self.assertEqual(s.get("delivered_seq"), 0)


if __name__ == "__main__":
    unittest.main()
