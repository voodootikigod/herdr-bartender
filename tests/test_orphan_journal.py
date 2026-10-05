"""R10 orphan-lock contention on the event path, and the reconciler draining the contention journal.

Gap orphan-lock-blocking: the event path takes the orphan lock with a 50ms LOCK_NB deadline and,
when another process (--replay-orphans, --cleanup, the reconciler) holds it, journals the export
under ``<orphan file>.pending/`` and flags the reconciler, which drains the journal on every pass.
"""

import json
import threading
import time
import unittest
from unittest import mock

from herdr_bartender import background, orphans
from herdr_bartender.bridge import DeliveryResult
from herdr_bartender.handlers import handle_agent_status_changed, handle_pane_closed
from herdr_bartender.sender import step_b
from herdr_bartender.paths import get_orphan_path
from tests.support import SandboxTestCase
from tests.support.lock_holder import hold_lock

# A blocking orphan flock would wait for the holder (HOLD_SECONDS); the bounded path returns long before.
HOLD_SECONDS = 5.0
EVENT_PATH_BOUND_SECONDS = 0.5


def _working(pane: str) -> dict:
    return {"agent_status": "working", "pane_id": pane, "workspace_id": "w1", "agent": "claude", "tab_id": "w1:t1"}


def _reject(payload, bridge_url=None, timeout=0.2):
    return DeliveryResult("non_retryable", "4xx_client_error", 400)


class OrphanJournalCase(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.orphan_path = get_orphan_path()
        self.journal = orphans.pending_dir_for(self.orphan_path)

    def _hold_orphan_lock(self, seconds: float = HOLD_SECONDS):
        return hold_lock(self, orphans._lock_path(self.orphan_path), seconds=seconds)

    def _journaled(self, op: str = orphans.OP_EXPORT) -> list:
        entries = sorted(self.journal.glob("*.json")) if self.journal.is_dir() else []
        ops = [json.loads(entry.read_text()) for entry in entries]
        return [entry for entry in ops if entry.get("op") == op]

    def _orphan_sessions(self) -> dict:
        return json.loads(self.orphan_path.read_text())["sessions"] if self.orphan_path.exists() else {}


class OrphanCapacityTests(OrphanJournalCase):
    """Plan §9.2 item 1 ("bounded to 256 records"). Round-2 low finding: ORPHAN_CAPACITY was never exercised."""

    def test_orphan_file_keeps_the_newest_256_exports(self):
        self.assertEqual(orphans.ORPHAN_CAPACITY, 256)
        for i in range(300):
            sid = f"herdr:h:w1:p{i:04d}"
            orphans.export_orphan_record(sid, {"pane_id": f"w1:p{i:04d}", "desired_state": "Ended"}, blocking=True)
        kept = self._orphan_sessions()
        self.assertEqual(sorted(kept), [f"herdr:h:w1:p{i:04d}" for i in range(44, 300)])

    def test_re_export_of_a_full_files_oldest_record_moves_it_to_the_newest(self):
        """R27/R33 (round-3 finding): a fresh export of a session id already in the file replaces the record AND makes
        it the newest. It used to keep its oldest slot, so the next export over the cap evicted the just-refreshed
        owed Ended while 255 older records survived."""
        cap = orphans.ORPHAN_CAPACITY
        for i in range(cap):
            orphans.export_orphan_record(f"herdr:h:w1:p{i:04d}", {"pane_id": f"w1:p{i:04d}", "n": 0}, blocking=True)
        refreshed = "herdr:h:w1:p0000"
        orphans.export_orphan_record(refreshed, {"pane_id": "w1:p0000", "n": 1}, blocking=True)
        self.assertEqual(list(self._orphan_sessions())[-1], refreshed, "the fresh export is the newest in file order")
        orphans.export_orphan_record("herdr:h:w1:pNEW", {"pane_id": "w1:pNEW", "n": 0}, blocking=True)
        kept = self._orphan_sessions()
        self.assertEqual(len(kept), cap)
        self.assertEqual(kept[refreshed], {"pane_id": "w1:p0000", "n": 1}, "the refreshed owed Ended survives the cap")
        self.assertNotIn("herdr:h:w1:p0001", kept, "the oldest untouched record is the one evicted")
        self.assertEqual(list(kept)[-2:], [refreshed, "herdr:h:w1:pNEW"])

    def test_identical_re_export_still_moves_the_record_to_the_newest(self):
        """The file is rewritten when only the order changed (dict equality ignores order)."""
        orphans.export_orphan_record("herdr:h:w1:pA", {"pane_id": "w1:pA"}, blocking=True)
        orphans.export_orphan_record("herdr:h:w1:pB", {"pane_id": "w1:pB"}, blocking=True)
        orphans.export_orphan_record("herdr:h:w1:pA", {"pane_id": "w1:pA"}, blocking=True)
        self.assertEqual(list(self._orphan_sessions()), ["herdr:h:w1:pB", "herdr:h:w1:pA"])

    def test_journal_fold_respects_the_256_cap(self):
        """Exports queued in the R10 journal are folded oldest first; the file still keeps only the newest 256."""
        orphans.journal_orphan_exports((f"herdr:h:w1:j{i:04d}", {"pane_id": f"w1:j{i:04d}"}) for i in range(260))
        self.assertTrue(orphans.flush_pending_orphan_ops())
        self.assertEqual(sorted(self._orphan_sessions()), [f"herdr:h:w1:j{i:04d}" for i in range(4, 260)])


class EventPathOrphanContentionTests(OrphanJournalCase):
    """R10: a handler's Step C orphan export never blocks on a contended orphan lock."""

    def _assert_journaled_not_written(self, sid: str, elapsed: float) -> None:
        self.assertLess(elapsed, EVENT_PATH_BOUND_SECONDS, "the event path blocked on the contended orphan lock")
        self.assertEqual([op["sid"] for op in self._journaled()], [sid], "the contended export must be journaled")
        self.assertIs(self._journaled()[0]["session"]["orphaned_ended"], True)
        self.assertFalse(self.orphan_path.exists(), "nothing may be written to the orphan file without its lock")
        self.assertTrue((self.state_dir / "reconciler.pending").exists(), "the journal is left to the reconciler")
        with self.cache_mgr as data:
            self.assertIs(data["sessions"][sid]["orphaned_ended"], True)

    def test_status_agent_exit_rejection_journals_under_orphan_contention(self):
        pane = "w1:pExit"
        handle_agent_status_changed(_working(pane), {}, bridge_url=self.mock_url)
        self._hold_orphan_lock()
        agent_exit = {**_working(pane), "agent_status": "idle", "agent": ""}
        with mock.patch.object(step_b, "send_event", side_effect=_reject) as sent:
            t0 = time.monotonic()
            handle_agent_status_changed(agent_exit, {}, bridge_url=self.mock_url)
            elapsed = time.monotonic() - t0
        self.assertEqual([call.args[0]["state"] for call in sent.call_args_list], ["Ended", "Ended"],
                         "primary Ended + the single minimal retry after a rejection (Plan §3.3)")
        self._assert_journaled_not_written(self.sid(pane), elapsed)

    def test_pane_closed_rejection_journals_under_orphan_contention(self):
        pane = "w1:pClose"
        handle_agent_status_changed(_working(pane), {}, bridge_url=self.mock_url)
        self._hold_orphan_lock()
        with mock.patch.object(step_b, "send_event", side_effect=_reject) as sent:
            t0 = time.monotonic()
            handle_pane_closed({"pane_id": pane}, {}, bridge_url=self.mock_url)
            elapsed = time.monotonic() - t0
        self.assertEqual([call.args[0]["state"] for call in sent.call_args_list], ["Ended", "Ended"],
                         "primary Ended + the single minimal retry after a rejection (Plan §3.3)")
        self._assert_journaled_not_written(self.sid(pane), elapsed)


class ReconcilerDrainsJournalTests(OrphanJournalCase):
    """Gap orphan-lock-blocking ('have the reconciler drain those flags'): every pass flushes the journal."""

    def _journal_contended_export(self, sid: str, record: dict) -> None:
        holder = self._hold_orphan_lock()
        self.assertIs(orphans.export_orphan_record(sid, record), False)
        holder.release()
        self.assertFalse(self.orphan_path.exists())
        self.assertEqual(len(self._journaled()), 1)

    def test_sweep_pass_flushes_the_journal_into_the_orphan_file(self):
        sid = self.sid("w1:pJournal")
        record = {"agent": "claude", "pane_id": "w1:pJournal", "desired_state": "Ended", "orphaned_ended": True}
        self._journal_contended_export(sid, record)
        self.bridge.health_ok = False  # no automatic replay: the flushed record must stay in the orphan file
        background._sweep_pass(self.state_dir, self.cache_mgr, self.mock_url)
        self.assertEqual(self._orphan_sessions(), {sid: record})
        self.assertEqual(self._journaled(), [], "the flushed journal entries are removed")

    def test_flushed_export_is_replayed_in_the_same_pass(self):
        """The flush runs before the automatic orphan replay, so a healthy bridge dismisses the phantom at once."""
        sid = self.sid("w1:pJournal")
        self.bridge.sessions[sid] = {"state": "Working"}
        self._journal_contended_export(sid, {"agent": "claude", "pane_id": "w1:pJournal", "desired_state": "Ended"})
        background._sweep_pass(self.state_dir, self.cache_mgr, self.mock_url)
        self.assertEqual([e["state"] for e in self.bridge.events_for(sid)], ["Ended"])
        self.assertNotIn(sid, self.bridge.sessions)
        self.assertFalse(self.orphan_path.exists())
        self.assertEqual(self._journaled(), [])

    def test_contended_handler_export_reaches_the_orphan_file_on_the_next_pass(self):
        """End to end: a rejected agent-exit Ended journaled under contention is visible to --replay-orphans and
        rollback after one reconciler pass, without any unrelated orphan export."""
        pane = "w1:pE2E"
        handle_agent_status_changed(_working(pane), {}, bridge_url=self.mock_url)
        holder = self._hold_orphan_lock()
        with mock.patch.object(step_b, "send_event", side_effect=_reject):
            handle_agent_status_changed({**_working(pane), "agent_status": "idle", "agent": ""}, {},
                                        bridge_url=self.mock_url)
        holder.release()
        self.bridge.health_ok = False
        background._sweep_pass(self.state_dir, self.cache_mgr, self.mock_url)
        self.assertIn(self.sid(pane), self._orphan_sessions())
        self.assertEqual(self._journaled(), [])

    def test_empty_journal_does_not_wait_for_the_orphan_lock(self):
        """Once drained, the journal directory stays behind; a pass must not then queue behind --replay-orphans."""
        self._journal_contended_export(self.sid("w1:pJournal"), {"agent": "claude", "pane_id": "w1:pJournal"})
        self.assertIs(orphans.flush_pending_orphan_ops(), True)
        self.assertTrue(self.journal.is_dir())
        self._hold_orphan_lock()
        t0 = time.monotonic()
        self.assertIs(orphans.flush_pending_orphan_ops(blocking=True), True)
        self.assertLess(time.monotonic() - t0, EVENT_PATH_BOUND_SECONDS, "an empty journal took the blocking lock")

    def test_final_absence_export_waits_for_the_orphan_lock(self):
        """R10: the reconciler's last export before it exits (Bartender absent > 12h) may block on the orphan lock;
        it must not journal the record and leave it behind after the reconciler is gone."""
        pane = "w1:pAbsent"
        handle_agent_status_changed(_working(pane), {}, bridge_url=self.mock_url)
        holder = self._hold_orphan_lock()
        releaser = threading.Timer(0.2, holder.release)
        releaser.start()
        self.addCleanup(releaser.join)
        background._export_undelivered(self.cache_mgr)
        self.assertIn(self.sid(pane), self._orphan_sessions())
        self.assertEqual(self._journaled(), [])


if __name__ == "__main__":
    unittest.main()
