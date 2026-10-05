"""Owed compensating Ended entries: bounded retry schedule, no reconciler spin, exhaustion to the orphan file.

Finding (reconciler spin): a compensation the bridge does not confirm used to re-flag ``reconciler.pending`` on every
drain, so the background loop re-ran its sweep with no sleep and POSTed the Ended continuously. An owed entry now
keeps the loop alive WITHOUT re-flagging it, is retried at 0/1/2/4/8s (``attempts`` / ``last_attempt`` persisted
under the re-verification lock) and, after ``MAX_COMPENSATION_ATTEMPTS`` unconfirmed POSTs, is exported to the orphan
file (replayed once the bridge is healthy) and dropped from the cache.
"""

import json
import unittest
from unittest import mock

from herdr_bartender import background, clock
from herdr_bartender.orphans import journaled_export_sids
from herdr_bartender.paths import get_orphan_path
from herdr_bartender.reconciler import reconcile_active_sessions
from herdr_bartender.sender.compensation import (
    COMPENSATION_RETRY_DELAYS,
    MAX_COMPENSATION_ATTEMPTS,
    compensation_wait,
    next_compensation_wait,
)
from tests.support import SandboxTestCase

PANE = "w1:pOwed"
PASS_GUARD = 40


class OwedCompensationCase(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.clock = self.use_fake_clock()
        self.session_id = self.sid(PANE)
        self.bridge.probe = lambda: {"at": clock.time()}

    def seed(self, **bookkeeping):
        entry = {"session_id": self.session_id, "pane_id": PANE, "agent": "Claude (Herdr)", "generation": 2,
                 "admitted_at_ns": 1, "timestamp": clock.time(), **bookkeeping}
        with self.cache_mgr as data:
            data["pending_compensations"] = [entry]
            self.cache_mgr.save(data)
        return entry

    def owed(self):
        with self.cache_mgr as data:
            return list(data["pending_compensations"])

    def compensation_posts(self):
        return [r for r in self.bridge.requests
                if r["method"] == "POST" and (r["body"] or {}).get("session_id") == self.session_id]

    def run_loop(self):
        """run_reconcile_background with a guard: a busy loop fails the test instead of hanging it.

        The loop keeps running while a session or an orphan export waits on an unhealthy bridge, so the run is
        ended (DISABLED) after the first pass that starts with the owed compensation settled one way or the other.
        """
        passes = []
        real_pass = background._sweep_pass

        def guarded(*args):
            passes.append(clock.monotonic())
            if len(passes) > PASS_GUARD:
                raise AssertionError(f"reconciler spin: {len(passes)} sweep passes")
            settled = not self.owed()
            result = real_pass(*args)
            if settled:
                (self.state_dir / "DISABLED").touch()
            return result

        with mock.patch.object(background, "_sweep_pass", side_effect=guarded):
            background.run_reconcile_background(bridge_url=self.mock_url)
        return passes


class ReconcilerSpinTests(OwedCompensationCase):
    def test_unconfirmed_compensation_is_retried_on_the_schedule_then_orphaned(self):
        """A bridge answering 500: exactly MAX attempts at 0/1/3/7/15s (delays 0,1,2,4,8), never a busy loop; the
        loop stays alive between attempts (no idle exit), then exports the Ended to the orphan file and exits."""
        self.bridge.return_code = 500
        self.bridge.health_ok = False  # keep the automatic orphan replay out of the POST count
        start = clock.time()
        self.seed()
        passes = self.run_loop()
        posts = self.compensation_posts()
        self.assertEqual(len(posts), MAX_COMPENSATION_ATTEMPTS, [r["at"] - start for r in posts])
        offsets = [round(r["at"] - start, 1) for r in posts]
        expected, at = [], 0.0
        for delay in COMPENSATION_RETRY_DELAYS:
            at += delay
            expected.append(at)
        for got, due in zip(offsets, expected):
            self.assertGreaterEqual(got, due)
            self.assertLess(got, due + 1.0, offsets)
        self.assertLess(len(passes), PASS_GUARD)
        self.assertEqual(self.owed(), [])
        orphans = json.loads(get_orphan_path().read_text())["sessions"]
        self.assertEqual(orphans[self.session_id]["desired_state"], "Ended")
        self.assertEqual(orphans[self.session_id]["pane_id"], PANE)
        self.assertFalse((self.state_dir / "reconciler.pending").exists())

    def test_compensation_confirmed_after_the_bridge_recovers(self):
        """Two 500s, then the bridge accepts: three POSTs, the entry is cleared and nothing is orphaned."""
        self.bridge.enqueue(500)
        self.bridge.enqueue(500)
        self.seed()
        self.run_loop()
        self.assertEqual([r["status"] for r in self.compensation_posts()], [500, 500, 200])
        self.assertEqual(self.owed(), [])
        self.assertFalse(get_orphan_path().exists())

    def test_drain_of_an_entry_not_yet_due_neither_posts_nor_reflags(self):
        """An entry whose last attempt was 0.5s ago is not due (1s delay): no POST and no reconciler.pending (the
        flag is what made the loop re-run with no sleep)."""
        self.seed(attempts=1, last_attempt=clock.time() - 0.5, posted=True)
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertEqual(self.compensation_posts(), [])
        self.assertFalse((self.state_dir / "reconciler.pending").exists())
        (entry,) = self.owed()
        self.assertEqual(entry["attempts"], 1)


    def test_exhausted_entry_is_exported_only_once_the_last_delay_passed(self):
        """Low finding (timing not pinned): after the 5th unconfirmed POST the entry is exported to the orphan journal
        COMPENSATION_RETRY_DELAYS[-1] seconds later - not earlier (an export that skips the last wait gives the 5th
        POST no chance to be confirmed by the bridge's own retries) - and without a 6th POST."""
        last_delay = COMPENSATION_RETRY_DELAYS[-1]
        self.seed(attempts=MAX_COMPENSATION_ATTEMPTS, last_attempt=clock.time(), posted=True)
        self.clock.advance(last_delay - 0.5)
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertEqual(len(self.owed()), 1, "still owed before the last delay passed")
        self.assertNotIn(self.session_id, journaled_export_sids())
        self.clock.advance(0.5)
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertEqual(self.owed(), [])
        self.assertIn(self.session_id, journaled_export_sids())
        self.assertEqual(self.compensation_posts(), [], "an exhausted entry is never POSTed again")

    def test_exhausted_entry_whose_orphan_export_fails_stays_owed(self):
        """Round-3 finding: when the orphan journal cannot be written (ENOSPC/EACCES) an exhausted compensating Ended
        is kept owed with a refreshed last_attempt - never dropped (a phantom Working would stay on Top Shelf) and
        never POSTed again. A later pass whose export works journals it and drops it."""
        last_delay = COMPENSATION_RETRY_DELAYS[-1]
        self.seed(attempts=MAX_COMPENSATION_ATTEMPTS, last_attempt=clock.time() - 100, posted=True)
        from herdr_bartender.sender import dispatch
        failed_at = clock.time()
        with mock.patch.object(dispatch, "journal_orphan_exports", side_effect=OSError(28, "No space left on device")):
            reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        (entry,) = self.owed()
        self.assertEqual((entry["attempts"], entry["last_attempt"]), (MAX_COMPENSATION_ATTEMPTS, failed_at))
        self.assertNotIn(self.session_id, journaled_export_sids())
        self.assertFalse(get_orphan_path().exists())
        self.clock.advance(last_delay)
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertEqual(self.owed(), [])
        exported = set(journaled_export_sids())
        if get_orphan_path().exists():   # the pass may already have folded the journal into the file
            exported |= set(json.loads(get_orphan_path().read_text())["sessions"])
        self.assertIn(self.session_id, exported)
        self.assertEqual(self.compensation_posts(), [], "an exhausted entry is never POSTed again")

    def test_malformed_entries_are_purged_not_kept_as_work(self):
        """An entry that can never be sent (no session id) would keep the loop awake forever: the drain drops it."""
        with self.cache_mgr as data:
            data["pending_compensations"] = ["junk", {"pane_id": PANE}]
            self.cache_mgr.save(data)
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertEqual(self.owed(), [])
        self.assertEqual(self.compensation_posts(), [])


class ScheduleTests(unittest.TestCase):
    def test_wait_follows_the_delays_and_tolerates_clock_steps(self):
        entry = {"session_id": "herdr:h:w1:p1"}
        self.assertEqual(compensation_wait(entry, 100.0), 0.0)
        for attempts, delay in enumerate(COMPENSATION_RETRY_DELAYS[1:], start=1):
            stamped = dict(entry, attempts=attempts, last_attempt=100.0)
            self.assertEqual(compensation_wait(stamped, 100.0), delay)
            self.assertEqual(compensation_wait(stamped, 100.0 + delay), 0.0)
        stamped = dict(entry, attempts=2, last_attempt=500.0)
        self.assertEqual(compensation_wait(stamped, 100.0), 0.0, "a wall clock stepped back never stalls the entry")

    def test_next_wait_ignores_malformed_entries(self):
        self.assertIsNone(next_compensation_wait([], 1.0))
        self.assertIsNone(next_compensation_wait(["junk", {"pane_id": "x"}], 1.0))
        entries = [{"session_id": "a", "attempts": 3, "last_attempt": 0.0}, {"session_id": "b", "attempts": 1,
                                                                           "last_attempt": 0.0}]
        self.assertEqual(next_compensation_wait(entries, 0.5), 0.5)


if __name__ == "__main__":
    unittest.main()
