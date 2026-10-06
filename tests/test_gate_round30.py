"""npx adversarial-review gate round 30: persisted owed work always wakes the reconciler; the orphan journal is
bounded (one entry per session, newest wins, hard ceiling) (R78)."""

import json
import time
import unittest
from unittest import mock

from herdr_bartender import orphans
from herdr_bartender.orphans import (
    _lock_path,
    export_orphan_record,
    journal_orphan_exports,
    journaled_export_records,
    pending_dir_for,
)
from herdr_bartender.paths import get_orphan_path
from tests.support import SandboxTestCase
from tests.support.lock_holder import hold_lock
from tests.support.reconciler_fixtures import seed, session


class JournalBoundTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        hold_lock(self, _lock_path(get_orphan_path()), seconds=30)   # every export is journaled
        self.pending = pending_dir_for(get_orphan_path())

    def entries(self):
        return sorted(self.pending.glob("*.json"))

    def test_newest_op_per_session_supersedes_older_entries(self):
        sid = "herdr:h:w1:pJ1"
        export_orphan_record(sid, {"pane_id": "w1:pJ1", "seq": 1, "desired_state": "Ended"})
        export_orphan_record(sid, {"pane_id": "w1:pJ1", "seq": 2, "desired_state": "Ended"})
        self.assertEqual(len(self.entries()), 1)
        self.assertEqual(journaled_export_records()[sid][0]["seq"], 2)

    def test_newer_op_wins_even_when_the_clock_stepped_back(self):
        """R80: a backward wall-clock step gives the newer op the smaller filename; it still supersedes the older."""
        sid = "herdr:h:w1:pClock"
        with mock.patch.object(orphans.clock, "time_ns", return_value=200_000_000_000):
            export_orphan_record(sid, {"pane_id": "w1:pClock", "seq": 1, "desired_state": "Ended"})
        with mock.patch.object(orphans.clock, "time_ns", return_value=100_000_000_000):
            export_orphan_record(sid, {"pane_id": "w1:pClock", "seq": 2, "desired_state": "Ended"})
        self.assertEqual(len(self.entries()), 1)
        self.assertEqual([r["seq"] for r in journaled_export_records()[sid]], [2])

    def test_distinct_sessions_stop_at_the_ceiling(self):
        with mock.patch.object(orphans, "JOURNAL_MAX_ENTRIES", 3):
            for n in range(3):
                export_orphan_record(f"herdr:h:w1:pC{n}", {"pane_id": f"w1:pC{n}", "seq": 1})
            self.assertFalse(export_orphan_record("herdr:h:w1:pC9", {"pane_id": "w1:pC9", "seq": 1}))
            export_orphan_record("herdr:h:w1:pC1", {"pane_id": "w1:pC1", "seq": 2})   # a known session still updates
            with self.assertRaises(OSError):
                journal_orphan_exports([("herdr:h:w1:pC8", {"pane_id": "w1:pC8", "seq": 1})])
        self.assertEqual(len(self.entries()), 3)
        self.assertNotIn("herdr:h:w1:pC9", journaled_export_records())


class JournalCausalOrderTests(SandboxTestCase):
    """R81: one atomically replaced file per session plus a lock-ordered sequence number: no crash or clock step can
    make an older op replay after a newer one."""

    def setUp(self):
        super().setUp()
        self.orphan = get_orphan_path()
        self.pending = pending_dir_for(self.orphan)

    def test_crash_leftover_older_format_entry_replays_first(self):
        sid = "herdr:h:w1:pCrash"
        key = orphans._journal_key(sid)
        self.pending.mkdir(parents=True, exist_ok=True)
        # An R78-format entry the crash left behind, with a wall-clock name from the future...
        (self.pending / f"99999999999999999999-1-000000-aa-k{key}.json").write_text(
            json.dumps({"op": "export", "sid": sid, "session": {"pane_id": "w1:pCrash", "seq": 1}}))
        # ...and the newer R81 entry for the same session.
        (self.pending / f"k{key}.json").write_text(
            json.dumps({"op": "export", "sid": sid, "session": {"pane_id": "w1:pCrash", "seq": 2}, "jseq": 1}))
        self.assertTrue(orphans.flush_pending_orphan_ops())
        self.assertEqual(orphans.read_orphan_records(self.orphan)[sid]["seq"], 2, "the newer op wins")

    def test_cross_session_order_follows_causality_not_the_clock(self):
        hold_lock(self, _lock_path(self.orphan), seconds=30)
        with mock.patch.object(orphans.clock, "time_ns", return_value=200_000_000_000):
            export_orphan_record("herdr:h:w1:pFirst", {"pane_id": "w1:pFirst", "seq": 1})
        with mock.patch.object(orphans.clock, "time_ns", return_value=100_000_000_000):
            export_orphan_record("herdr:h:w1:pSecond", {"pane_id": "w1:pSecond", "seq": 1})
        ops = orphans._load_journal(self.orphan).ops
        self.assertEqual([op["sid"] for op in ops], ["herdr:h:w1:pFirst", "herdr:h:w1:pSecond"])


class OwedWorkWakesReconcilerTests(SandboxTestCase):
    def test_ignored_event_wakes_the_reconciler_for_persisted_owed_work(self):
        """R78: only Ended sessions are cached, but a compensation is owed (a crash lost its hand-off). Even an event
        ignored at intake starts the reconciler."""
        sid = self.sid("w1:pOwed")
        seed(self.cache_mgr, {sid: session("w1:pOwed", "Ended", seq=3, now=time.time())})
        with self.cache_mgr as data:
            data["pending_compensations"] = [{"session_id": sid, "pane_id": "w1:pOwed", "agent": "Herdr",
                                              "generation": 1, "admitted_at_ns": 1, "timestamp": time.time()}]
            self.cache_mgr.save(data)
        shell = {"event": "pane.agent_status_changed",
                 "data": {"pane_id": "w1:pShell", "workspace_id": "w1", "agent_status": "working"}}
        res = self.run_cli("pane.agent_status_changed", input=json.dumps(shell).encode())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(self.subprocess_spawns(), "the reconciler is started for the owed compensation")


if __name__ == "__main__":
    unittest.main()
