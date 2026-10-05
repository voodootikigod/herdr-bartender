"""Gate finding (adversarial review, data-loss): an ``orphaned_ended`` mark saved while its orphan export is NOT durable.

Step C and the results drain journal the owed Ended exports before the save that marks them ``orphaned_ended``
(``journal_owed_exports``); --cleanup does the same before its marking save. When that journal fails (and the
export after the save fails too) the cache record is the only copy of the unconfirmed Ended, so the 256-cap prune
must not treat it as mirrored: such a record is flagged ``orphan_mirror_owed`` and stays until an export is
confirmed (the R12 horizon eviction, or a Step A cap prune that journals it first).
"""

import json
import time
import unittest
from unittest import mock

from herdr_bartender import cache, orphans
from herdr_bartender.bridge import DeliveryResult
from herdr_bartender.cleanup import EXIT_UNCONFIRMED, run_cleanup
from herdr_bartender.handlers import handle_agent_status_changed, handle_pane_closed
from herdr_bartender.lifecycle import ORPHAN_HORIZON_SECONDS, horizon_exports
from herdr_bartender.reconciler import evict_exported_sessions, reconcile_active_sessions
from herdr_bartender.results import drain_results_dir
from herdr_bartender.sender import step_b
from tests.support import SandboxTestCase
from tests.support.lock_holder import hold_lock

PANE = "w1:pMirror"
WORKING = {"agent_status": "working", "pane_id": PANE, "workspace_id": "w1", "agent": "claude", "tab_id": "w1:t1"}


def reject(payload, bridge_url=None, timeout=0.2):
    return DeliveryResult("non_retryable", "4xx_client_error", 400)


def unreachable(payload, bridge_url=None, timeout=0.2):
    return DeliveryResult("retryable", "network_timeout", None)


class _Crash(BaseException):
    pass


class MirrorOwedCase(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.sid_ = self.sid(PANE)

    def _record(self) -> dict:
        with self.cache_mgr as data:
            return dict(data["sessions"][self.sid_])

    def _mirrors(self) -> list:
        """Every copy of this session in the orphan file or in its R10 journal."""
        path = orphans.get_orphan_path()
        exported = json.loads(path.read_text())["sessions"] if path.exists() else {}
        journal = orphans.pending_dir_for(path)
        ops = [json.loads(p.read_text()) for p in journal.glob("*.json")] if journal.is_dir() else []
        copies = [exported[self.sid_]] if self.sid_ in exported else []
        return copies + [op["session"] for op in ops if op.get("sid") == self.sid_ and op.get("op") == "export"]

    def _orphaned(self) -> bool:
        return bool(self._mirrors())

    def _orphan_io_down(self):
        """The orphan file AND its journal are unwritable (disk full / permissions): every export fails."""
        down = OSError(28, "No space left on device")
        return mock.patch.multiple(orphans, _journal_op=mock.Mock(side_effect=down),
                                   _commit_locked=mock.Mock(side_effect=down))

    def assert_kept_until_exported(self, record: dict) -> None:
        self.assertIs(record["orphaned_ended"], True)
        self.assertFalse(self._orphaned(), "control: the export really failed")
        self.assertFalse(cache.safe_to_evict(record), "an unmirrored orphaned_ended must not be cap-evictable")
        filler = {f"herdr:h:w9:p{i}": {"desired_state": "Ended", "seq": 1, "delivered_seq": 1,
                                       "last_event_at": time.time() + i} for i in range(3)}
        pruned = cache.sessions_to_prune({self.sid_: {**record, "last_event_at": 0}, **filler}, cap=2)
        self.assertNotIn(self.sid_, pruned, "the oldest record, but its Ended exists nowhere else")
        self.assertEqual(len(pruned), 2, "control: the cap still prunes the confirmed Endeds")


class StepCMirrorOwedTests(MirrorOwedCase):
    def test_step_c_rejection_with_orphan_io_down_is_not_cap_evictable(self):
        handle_agent_status_changed(WORKING, {}, bridge_url=self.mock_url)
        with mock.patch.object(step_b, "send_event", side_effect=reject), self._orphan_io_down():
            handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url)
        self.assert_kept_until_exported(self._record())

    def test_results_drain_rejection_with_orphan_io_down_is_not_cap_evictable(self):
        handle_agent_status_changed(WORKING, {}, bridge_url=self.mock_url)
        holders = []

        def reject_then_contend(payload, bridge_url=None, timeout=0.2):
            if not holders:
                holders.append(hold_lock(self, self.cache_mgr.lock_file))
            return reject(payload)

        with mock.patch.object(step_b, "send_event", side_effect=reject_then_contend):
            handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url)
        holders[0].release()
        self.assertEqual(len(list((self.state_dir / "results").glob("*.json"))), 1, "control: Step C deferred")
        with self._orphan_io_down():
            drain_results_dir(self.state_dir)
        self.assert_kept_until_exported(self._record())

    def test_a_later_durable_export_journal_makes_it_evictable_again(self):
        """A second rejection of the same Ended, journaled this time, clears the flag (the export is durable)."""
        handle_agent_status_changed(WORKING, {}, bridge_url=self.mock_url)
        with mock.patch.object(step_b, "send_event", side_effect=reject), self._orphan_io_down():
            handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url)
        self.assertFalse(cache.safe_to_evict(self._record()))
        with self.cache_mgr as data:   # the reconciler re-arms it after a /health recovery; rejected again
            data["sessions"][self.sid_].update({"delivery_status": "in_flight", "next_retry_at": None})
            self.cache_mgr.save(data)
        from herdr_bartender.reconciler import deliver_due_sessions
        with mock.patch.object(step_b, "send_event", side_effect=reject):
            deliver_due_sessions(self.cache_mgr, bridge_url=self.mock_url)
        record = self._record()
        self.assertTrue(self._orphaned())
        self.assertTrue(cache.safe_to_evict(record))
        self.assertNotIn(cache.ORPHAN_MIRROR_OWED, record)
        for copy in self._mirrors():
            self.assertNotIn(cache.ORPHAN_MIRROR_OWED, copy, "the cache-only flag is not mirrored")

    def test_a_new_turn_drops_the_flag(self):
        handle_agent_status_changed(WORKING, {}, bridge_url=self.mock_url)
        with mock.patch.object(step_b, "send_event", side_effect=reject), self._orphan_io_down():
            handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url)
        self.assertIs(self._record().get(cache.ORPHAN_MIRROR_OWED), True)
        with self.cache_mgr as data:   # the pane's tombstone window is over: a new turn may be admitted
            data["tombstones"].pop(PANE, None)
            self.cache_mgr.save(data)
        handle_agent_status_changed({**WORKING, "timestamp": time.time() + 5}, {}, bridge_url=self.mock_url)
        record = self._record()
        self.assertEqual(record["desired_state"], "Working")
        self.assertNotIn(cache.ORPHAN_MIRROR_OWED, record)
        self.assertNotIn("orphaned_ended", record)

    def test_horizon_eviction_exports_it_once_the_orphan_file_is_writable(self):
        """The R12 horizon pass still evicts it, but only after the export really landed."""
        handle_agent_status_changed(WORKING, {}, bridge_url=self.mock_url)
        with mock.patch.object(step_b, "send_event", side_effect=reject), self._orphan_io_down():
            handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            exports = horizon_exports(data, time.time() + ORPHAN_HORIZON_SECONDS + 60)
        self.assertEqual([sid for sid, _ in exports], [self.sid_])
        with self._orphan_io_down():
            self.assertEqual(evict_exported_sessions(self.cache_mgr, exports, "horizon"), ())
        self.assertIn(self.sid_, self._read_sessions(), "not written: kept for the next pass")
        self.assertEqual(evict_exported_sessions(self.cache_mgr, exports, "horizon"), (self.sid_,))
        self.assertTrue(self._orphaned())
        self.assertNotIn(self.sid_, self._read_sessions())

    def _read_sessions(self) -> dict:
        with self.cache_mgr as data:
            return dict(data["sessions"])


class ReconcilerRemirrorTests(MirrorOwedCase):
    """Gate finding (review round 4): the flag was never cleared once an export DID land after the save, so the
    record stayed out of the cap prune (and of a spool replay's capacity pruning) until the 12h horizon."""

    def _reject_with_journal_down(self, export_down: bool) -> None:
        handle_agent_status_changed(WORKING, {}, bridge_url=self.mock_url)
        down = OSError(28, "No space left on device")
        patches = {"_journal_op": mock.Mock(side_effect=down)}
        if export_down:
            patches["_commit_locked"] = mock.Mock(side_effect=down)
        with mock.patch.object(step_b, "send_event", side_effect=reject), mock.patch.multiple(orphans, **patches):
            handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url)
        self.assertIs(self._record().get(cache.ORPHAN_MIRROR_OWED), True)

    def test_step_c_hands_off_to_the_reconciler_when_the_journal_failed(self):
        self._reject_with_journal_down(export_down=True)
        self.assertTrue((self.state_dir / "reconciler.pending").exists(), "the flag needs a reconciler pass")

    def test_reconciler_pass_re_exports_and_clears_the_flag(self):
        self._reject_with_journal_down(export_down=True)
        self.assertFalse(self._orphaned(), "control: nothing durable yet")
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        record = self._record()
        self.assertTrue(self._orphaned())
        self.assertNotIn(cache.ORPHAN_MIRROR_OWED, record)
        self.assertTrue(cache.safe_to_evict(record), "mirrored now: the cap may prune it again")

    def test_export_that_landed_after_the_save_clears_the_flag_on_the_next_pass(self):
        self._reject_with_journal_down(export_down=False)
        self.assertTrue(self._orphaned(), "control: the post-save export landed in the orphan file")
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertNotIn(cache.ORPHAN_MIRROR_OWED, self._record())

    def test_orphan_io_still_down_keeps_the_flag_and_journals_nothing_new(self):
        self._reject_with_journal_down(export_down=True)
        with self._orphan_io_down():
            reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
            reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertIs(self._record().get(cache.ORPHAN_MIRROR_OWED), True)
        self.assertFalse(self._orphaned())

    def test_journaled_export_counts_as_durable_without_journaling_it_again(self):
        """The orphan FILE is unwritable but the journal works: an export already waiting in the journal is durable
        as it is, so the pass clears the flag without journaling the same export once more."""
        self._reject_with_journal_down(export_down=True)
        orphans.journal_orphan_exports([(self.sid_, self._record())])   # e.g. a contended export, journaled
        journal = orphans.pending_dir_for(orphans.get_orphan_path())
        self.assertEqual(len(list(journal.glob("*.json"))), 1)
        with mock.patch.object(orphans, "_commit_locked", side_effect=OSError(13, "Permission denied")):
            reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertNotIn(cache.ORPHAN_MIRROR_OWED, self._record())
        self.assertEqual(len(list(journal.glob("*.json"))), 1, "the waiting export is not journaled twice")


class CleanupMirrorTests(MirrorOwedCase):
    def setUp(self):
        super().setUp()
        with self.cache_mgr as data:
            data["sessions"][self.sid_] = {"desired_state": "Working", "seq": 1, "delivered_seq": 1, "pane_id": PANE,
                                           "agent": "Claude (Herdr)", "last_event_at": time.time()}
            self.cache_mgr.save(data)

    def test_crash_after_the_marking_save_keeps_the_owed_ended(self):
        """--cleanup marks this run's unconfirmed Endeds orphaned_ended; the export is journaled before that save."""
        real_save = cache.BoundedSessionCache.save

        def save(mgr, data, *args, **kwargs):
            real_save(mgr, data, *args, **kwargs)
            if any(isinstance(r, dict) and r.get("orphaned_ended") for r in data.get("sessions", {}).values()):
                raise _Crash("process killed after the save")

        with mock.patch.object(step_b, "send_event", side_effect=unreachable), \
                mock.patch.object(cache.BoundedSessionCache, "save", autospec=True, side_effect=save), \
                self.assertRaises(_Crash):
            run_cleanup(bridge_url=self.mock_url)
        self.assertIs(self._record()["orphaned_ended"], True)
        self.assertTrue(self._orphaned(), "orphaned_ended was saved but its export was not durable")

    def test_orphan_io_down_leaves_the_record_flagged(self):
        with mock.patch.object(step_b, "send_event", side_effect=unreachable), self._orphan_io_down():
            self.assertEqual(run_cleanup(bridge_url=self.mock_url), EXIT_UNCONFIRMED)
        self.assert_kept_until_exported(self._record())


if __name__ == "__main__":
    unittest.main()
