"""Pure Step A close staging: tombstone discipline and stable close origins (Plan §4.1, §4.3 Step A, item 5)."""

import subprocess
import time
import unittest

import copy

from herdr_bartender import cache
from herdr_bartender.cache_schema import new_cache
from herdr_bartender.handlers import handle_agent_status_changed, handle_pane_closed, handle_tab_closed
from herdr_bartender.intake import resolve_identity
from herdr_bartender.process import get_process_start_time
from herdr_bartender.staging import SESSION_CAP, stage_status
from tests.support import SandboxTestCase


class CloseStagingTests(SandboxTestCase):
    def _working(self, pane, arr_ns, tab="w1:t1"):
        handle_agent_status_changed({"agent_status": "working", "pane_id": pane, "workspace_id": pane.split(":")[0],
                                     "agent": "claude", "tab_id": tab}, {}, bridge_url=self.mock_url, arrival_ns=arr_ns)

    def _data(self):
        with self.cache_mgr as data:
            return data

    def test_stale_close_does_not_tombstone_readmitted_pane(self):
        """Gap tombstone-on-stale-close: a close predating the session's admission changes nothing."""
        t0 = time.time_ns()
        self._working("w1:pStale", t0)
        handle_pane_closed({"pane_id": "w1:pStale"}, {}, bridge_url=self.mock_url, arrival_ns=t0 - 1_000)
        data = self._data()
        self.assertNotIn("w1:pStale", data["tombstones"])
        self.assertEqual(data["sessions"][self.sid("w1:pStale")]["desired_state"], "Working")

    def test_close_without_sessions_still_persists_tombstone(self):
        """Gap tombstone-on-stale-close: the tombstone is saved even when no session is cached."""
        handle_pane_closed({"pane_id": "w1:pNoSession"}, {}, bridge_url=self.mock_url, arrival_ns=time.time_ns())
        self.assertIn("w1:pNoSession", self._data()["tombstones"])

    def test_duplicate_close_keeps_original_origin(self):
        """Plan §1 L86 / gap closed-source-ts-not-persisted: tab.closed then pane.closed (dual teardown) keeps the
        first closed_at_ns and closed_source_ts on the session and the tombstone."""
        t0 = time.time_ns()
        self._working("w1:pDual", t0)
        self.bridge.return_code = 500  # keep the session cached across both closes
        handle_tab_closed({"tab_id": "w1:t1"}, {}, bridge_url=self.mock_url, arrival_ns=t0 + 1_000)
        first = dict(self._data()["sessions"][self.sid("w1:pDual")])
        handle_pane_closed({"pane_id": "w1:pDual"}, {}, bridge_url=self.mock_url, arrival_ns=t0 + 9_000)
        data = self._data()
        session = data["sessions"][self.sid("w1:pDual")]
        self.assertEqual((session["closed_at_ns"], session["closed_source_ts"]),
                         (first["closed_at_ns"], first["closed_source_ts"]))
        self.assertEqual(data["tombstones"]["w1:pDual"]["closed_at_ns"], t0 + 1_000)
        self.assertGreater(session["seq"], first["seq"], "the duplicate close still re-sends Ended")

    def test_tombstone_source_ts_survives_step_c(self):
        """Gap closed-source-ts-not-persisted: Step C re-records the tombstone from the persisted closed_source_ts."""
        t0 = time.time_ns()
        self._working("w1:pSrc", t0)
        handle_pane_closed({"pane_id": "w1:pSrc", "timestamp": 12345.5}, {}, bridge_url=self.mock_url,
                           arrival_ns=t0 + 1_000)
        data = self._data()
        self.assertNotIn(self.sid("w1:pSrc"), data["sessions"])
        self.assertEqual(data["tombstones"]["w1:pSrc"]["closed_source_ts"], max(12345.5, (t0 + 1_000) / 1e9))

    def test_cascade_tombstones_panes_leased_elsewhere(self):
        """Gap cascade-tombstone-only-when-leased: a session leased by a live foreign sender is tombstoned too,
        so a later pre-close status cannot resurrect it."""
        t0 = time.time_ns()
        self._working("w1:pLeased", t0)
        holder = subprocess.Popen(["sleep", "30"])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        with self.cache_mgr as data:
            data["sessions"][self.sid("w1:pLeased")].update({
                "sending_pid": holder.pid, "lease_deadline": time.time() + 30,
                "lease_token": f"{holder.pid}:{get_process_start_time(holder.pid)}:0:x"})
            self.cache_mgr.save(data)
        posts = len(self.bridge.requests)
        handle_tab_closed({"tab_id": "w1:t1"}, {}, bridge_url=self.mock_url, arrival_ns=t0 + 1_000)
        self.assertEqual(len(self.bridge.requests), posts, "leased elsewhere: deferred, not sent")
        self.assertIn("w1:pLeased", self._data()["tombstones"])
        handle_agent_status_changed({"agent_status": "idle", "pane_id": "w1:pLeased", "workspace_id": "w1",
                                     "agent": "claude"}, {}, bridge_url=self.mock_url, arrival_ns=t0 + 2_000)
        self.assertEqual(self._data()["sessions"][self.sid("w1:pLeased")]["desired_state"], "Ended")

    def test_cascade_overflow_single_lock_and_unleased(self):
        """Plan §5.2 (gaps cascade-overflow-two-locks, cascade-reconciler-spawn-in-lock): one session inline; the rest
        are staged in_flight and unleased in the SAME Step A lock hold; the reconciler is flagged after unlock."""
        t0 = time.time_ns()
        for i in range(3):
            self._working(f"w1:pOver{i}", t0 + i)
        posts = len(self.bridge.requests)
        entered = []
        real_enter = cache.BoundedSessionCache.__enter__

        def counting_enter(mgr):
            entered.append(1)
            return real_enter(mgr)

        cache.BoundedSessionCache.__enter__ = counting_enter
        try:
            handle_tab_closed({"tab_id": "w1:t1"}, {}, bridge_url=self.mock_url, arrival_ns=t0 + 1_000)
        finally:
            cache.BoundedSessionCache.__enter__ = real_enter
        self.assertEqual(len(self.bridge.requests) - posts, 1, "exactly one inline Ended")
        self.assertEqual(len(entered), 2, "Step A + Step C only (no second Step A lock for overflow)")
        data = self._data()
        overflow = [s for s in data["sessions"].values() if s["desired_state"] == "Ended"]
        self.assertEqual(len(overflow), 2)
        for s in overflow:
            self.assertEqual((s["lease_token"] if "lease_token" in s else None, s["delivery_status"]), (None, "in_flight"))
        self.assertTrue((self.state_dir / "reconciler.pending").exists())


class CapacityStagingTests(SandboxTestCase):
    """Plan §4.3 Step A "Strict 256 Active Session Capacity Check" (gap capacity-prune-before-reject)."""

    start_bridge = False
    NEW_PANE = "wCap:pNew"

    def _full_cache(self, **extra):
        live = {f"herdr:{self.host}:wCap:p{i}": {"desired_state": "Working", "seq": 1, "pane_id": f"wCap:p{i}",
                                                  "last_event_at": 2000.0 + i}
                for i in range(SESSION_CAP - len(extra))}
        data = new_cache(self.host)
        data["sessions"] = {**live, **{self.sid(pane): rec for pane, rec in extra.items()}}
        self.assertEqual(len(data["sessions"]), SESSION_CAP)
        return data

    def _stage(self, data, agent_status="working"):
        event = {"agent_status": agent_status, "pane_id": self.NEW_PANE, "workspace_id": "wCap", "agent": "claude"}
        identity, _ = resolve_identity(event, {}, env={})
        return stage_status(data, identity, event, {}, time.time_ns(), herdr_alive=lambda: True)

    def test_new_pane_at_cap_prunes_salvaged_record_first(self):
        """255 live + 1 salvaged: the salvaged record is pruned and session 257 is admitted (cache stays at 256)."""
        data = self._full_cache(**{"wOld:pSalvaged": {"desired_state": "Idle", "seq": 1, "delivered_seq": 1,
                                                      "salvaged": True, "pane_id": "wOld:pSalvaged",
                                                      "last_event_at": 1.0}})
        stage = self._stage(data)
        self.assertTrue(stage.staged, stage.reason)
        self.assertIn(self.sid(self.NEW_PANE), data["sessions"])
        self.assertNotIn(self.sid("wOld:pSalvaged"), data["sessions"])
        self.assertEqual(len(data["sessions"]), SESSION_CAP)

    def test_new_pane_at_cap_prunes_confirmed_ended_record(self):
        """255 live + 1 Ended already delivered: the Ended record is pruned and session 257 is admitted."""
        data = self._full_cache(**{"wOld:pDone": {"desired_state": "Ended", "delivered_state": "Ended", "seq": 3,
                                                  "delivered_seq": 3, "pane_id": "wOld:pDone",
                                                  "last_event_at": 1.0}})
        self.assertTrue(self._stage(data).staged)
        self.assertNotIn(self.sid("wOld:pDone"), data["sessions"])
        self.assertEqual(len(data["sessions"]), SESSION_CAP)

    def test_new_pane_at_cap_never_evicts_live_or_undelivered_sessions(self):
        """256 live, or 255 live + 1 Ended still owed to Bartender (not orphaned): admission is refused and the
        cache is untouched (an undelivered Ended is never dropped to make room)."""
        cases = {
            "all live": self._full_cache(),
            "undelivered Ended": self._full_cache(**{"wOld:pOwed": {
                "desired_state": "Ended", "seq": 2, "delivered_seq": 1, "delivery_status": "in_flight",
                "pane_id": "wOld:pOwed", "last_event_at": 1.0}}),
        }
        for name, data in cases.items():
            with self.subTest(case=name):
                before = copy.deepcopy(data)
                stage = self._stage(data)
                self.assertFalse(stage.staged)
                self.assertIn("capacity", stage.reason)
                self.assertEqual(data, before)

    def test_dropped_event_at_cap_prunes_nothing(self):
        """Step A decides before it mutates: an event dropped after the capacity check (transient unknown status)
        leaves the evictable record in place."""
        data = self._full_cache(**{"wOld:pSalvaged": {"desired_state": "Idle", "seq": 1, "delivered_seq": 1,
                                                      "salvaged": True, "pane_id": "wOld:pSalvaged",
                                                      "last_event_at": 1.0}})
        before = copy.deepcopy(data)
        self.assertFalse(self._stage(data, agent_status="unknown").staged)
        self.assertEqual(data, before)


if __name__ == "__main__":
    unittest.main()
