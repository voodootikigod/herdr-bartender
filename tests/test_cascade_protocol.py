"""Container cascades through the Universal Sender (Plan §4.3 item 5, §5.2, §10.1 #3).

Gaps: test-cascade-overflow, t3-cascade-lease, cascade-no-budget-gate, cascade-reconciler-spawn-in-lock,
cascade-retryable-no-handoff, cascade-disabled-recheck.
"""

import time
import unittest

from herdr_bartender import clock, runtime
from herdr_bartender.handlers import handle_agent_status_changed, handle_tab_closed, handle_workspace_closed
from herdr_bartender.process import get_process_start_time, is_pid_alive, reset_caches
from herdr_bartender.reconciler import reconcile_active_sessions
from tests.support import SandboxTestCase


class CascadeCase(SandboxTestCase):
    def _admit(self, pane, tab, arr_ns=None):
        ws = pane.split(":")[0]
        handle_agent_status_changed({"agent_status": "working", "pane_id": pane, "workspace_id": ws, "agent": "claude",
                                     "tab_id": tab}, {}, bridge_url=self.mock_url, arrival_ns=arr_ns)

    def _data(self):
        with self.cache_mgr as data:
            return data

    def _ended_posts(self):
        return [b for b in self.bridge.arrivals if isinstance(b, dict) and b.get("state") == "Ended"]

    def _lease_to_live_holder(self, pane):
        holder = self.add_fake_process("other-sender", live=True)
        token = f"{holder}:{get_process_start_time(holder)}:{time.time()}:{self.sid(pane)}"
        with self.cache_mgr as data:
            data["sessions"][self.sid(pane)].update(
                {"lease_token": token, "sending_pid": holder, "lease_deadline": time.time() + 30})
            self.cache_mgr.save(data)
        return holder, token


class CascadeOverflowTests(CascadeCase):
    PANES = ("w1:pT0", "w1:pT1", "w1:pT2")

    def setUp(self):
        super().setUp()
        for pane in self.PANES:
            self._admit(pane, "w1:t1")
        self._admit("w2:pOther", "w2:t1")      # same raw tab id "t1", another workspace
        self.spawner.reset()

    def test_one_inline_ended_rest_unleased_in_flight_one_handoff(self):
        dead = next(pid for pid in range(999_999, 1_100_000) if not is_pid_alive(pid))
        with self.cache_mgr as data:  # an overflow session still carries a crashed sender's lease (row 3)
            data["sessions"][self.sid("w1:pT2")].update(
                {"lease_token": f"{dead}:None:1.0:x", "sending_pid": dead, "lease_deadline": time.time() + 30})
            self.cache_mgr.save(data)
        handle_tab_closed({"tab_id": "t1", "workspace_id": "w1"}, {}, bridge_url=self.mock_url)
        self.assertEqual(len(self._ended_posts()), 1, "Plan §5.2: at most 1 session inline")
        data = self._data()
        overflow = [data["sessions"][self.sid(p)] for p in self.PANES if self.sid(p) in data["sessions"]]
        self.assertEqual(len(overflow), 2)
        for s in overflow:
            self.assertEqual((s["desired_state"], s["seq"], s["delivery_status"]), ("Ended", 2, "in_flight"))
            self.assertEqual((s.get("lease_token"), s.get("sending_pid"), s.get("lease_deadline")), (None, None, None))
        for pane in self.PANES:
            self.assertIn(pane, data["tombstones"])
        self.assertEqual(data["sessions"][self.sid("w2:pOther")]["desired_state"], "Working")
        self.assertNotIn("w2:pOther", data["tombstones"])
        self.assertEqual(len(self.spawner.calls), 1, "a single reconciler hand-off")
        self.assertEqual(self.spawner.in_critical_section, [False], "spawned only after the lock was released")
        self.assertTrue((self.state_dir / "reconciler.pending").exists())

    def test_workspace_cascade_matches_only_its_workspace(self):
        handle_workspace_closed({"workspace_id": "w2"}, {}, bridge_url=self.mock_url)
        data = self._data()
        self.assertNotIn(self.sid("w2:pOther"), data["sessions"])
        for pane in self.PANES:
            self.assertEqual(data["sessions"][self.sid(pane)]["desired_state"], "Working")

    def test_live_foreign_lease_is_tombstoned_but_not_claimed(self):
        _, token = self._lease_to_live_holder("w1:pT0")
        handle_tab_closed({"tab_id": "w1:t1"}, {}, bridge_url=self.mock_url)
        data = self._data()
        s0 = data["sessions"][self.sid("w1:pT0")]
        self.assertEqual((s0["lease_token"], s0["desired_state"]), (token, "Ended"))
        self.assertIn("w1:pT0", data["tombstones"])
        self.assertNotIn(self.sid("w1:pT0"), [b["session_id"] for b in self._ended_posts()])
        self.assertEqual(len(self._ended_posts()), 1, "one of the free sessions went inline")

    def test_close_predating_admission_or_older_generation_is_ignored(self):
        admitted = self._data()["sessions"][self.sid("w1:pT0")]["admitted_at_ns"]
        handle_tab_closed({"tab_id": "w1:t1"}, {}, bridge_url=self.mock_url, arrival_ns=admitted - 1_000)
        handle_tab_closed({"tab_id": "w1:t1"}, {}, bridge_url=self.mock_url, spool_generation=0)
        data = self._data()
        for pane in self.PANES:
            self.assertEqual(data["sessions"][self.sid(pane)]["desired_state"], "Working")
            self.assertNotIn(pane, data["tombstones"])
        self.assertEqual(self._ended_posts(), [])


class CascadeLeaseAndBudgetTests(CascadeCase):
    def test_p03_live_lease_defers_then_reconciler_delivers(self):
        """Plan §10.1 #3: a session leased by a live foreign sender is deferred (lease kept, reconciler flagged);
        once that sender is gone the reconciler delivers the Ended and the tombstone stays recorded."""
        self._admit("w1:pL", "w1:tL")
        _, token = self._lease_to_live_holder("w1:pL")
        handle_tab_closed({"tab_id": "w1:tL"}, {}, bridge_url=self.mock_url)
        s = self._data()["sessions"][self.sid("w1:pL")]
        self.assertEqual((s["lease_token"], s["desired_state"]), (token, "Ended"))
        self.assertTrue((self.state_dir / "reconciler.pending").exists())
        self.assertEqual(self._ended_posts(), [])
        with self.cache_mgr as data:  # the holder's lease expires (deadline + grace long past: row 6)
            data["sessions"][self.sid("w1:pL")]["lease_deadline"] = time.time() - 10
            self.cache_mgr.save(data)
        reset_caches()
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        data = self._data()
        self.assertNotIn(self.sid("w1:pL"), data["sessions"])
        self.assertIn("w1:pL", data["tombstones"])
        self.assertEqual([b["session_id"] for b in self._ended_posts()], [self.sid("w1:pL")])

    def test_no_inline_post_without_budget(self):
        """Gap cascade-no-budget-gate: with time_remaining() <= 0.3s nothing is sent; the claimed session's lease
        is released and every Ended is left in_flight for the reconciler."""
        for pane in ("w1:pB0", "w1:pB1"):
            self._admit(pane, "w1:tB")
        self.spawner.reset()
        runtime.PROCESS_DEADLINE_SECONDS, runtime.START_TIME = 0.29, clock.monotonic()
        handle_tab_closed({"tab_id": "w1:tB"}, {}, bridge_url=self.mock_url)
        self.assertEqual(self._ended_posts(), [])
        data = self._data()
        for pane in ("w1:pB0", "w1:pB1"):
            s = data["sessions"][self.sid(pane)]
            self.assertEqual((s["desired_state"], s["delivery_status"], s.get("lease_token")), ("Ended", "in_flight", None))
        self.assertEqual(len(self.spawner.calls), 1)

    def test_retryable_inline_failure_hands_off(self):
        """Gap cascade-retryable-no-handoff: a 5xx on the inline Ended (and on its R45 minimal retry) flags and ensures
        the reconciler."""
        self._admit("w1:pR", "w1:tR")
        self.spawner.reset()
        self.bridge.enqueue(502)
        self.bridge.enqueue(502)
        handle_tab_closed({"tab_id": "w1:tR"}, {}, bridge_url=self.mock_url)
        s = self._data()["sessions"][self.sid("w1:pR")]
        self.assertEqual((s["delivery_error"], s["delivery_attempts"]), ("5xx_server_error", 1))
        self.assertTrue((self.state_dir / "reconciler.pending").exists())
        self.assertEqual(len(self.spawner.calls), 1)

    def test_disabled_rechecked_under_the_cascade_lock(self):
        """Gap cascade-disabled-recheck: DISABLED present -> no staging, no save, no POST."""
        self._admit("w1:pD", "w1:tD")
        before = self._data()["cache_seq"]
        (self.state_dir / "DISABLED").touch()
        handle_tab_closed({"tab_id": "w1:tD"}, {}, bridge_url=self.mock_url)
        handle_workspace_closed({"workspace_id": "w1"}, {}, bridge_url=self.mock_url)
        (self.state_dir / "DISABLED").unlink()
        data = self._data()
        self.assertEqual(data["cache_seq"], before)
        self.assertEqual(data["sessions"][self.sid("w1:pD")]["desired_state"], "Working")
        self.assertEqual(self._ended_posts(), [])


if __name__ == "__main__":
    unittest.main()
