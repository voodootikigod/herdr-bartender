"""Reconciler loop under cache failures: bounded backoff, no busy loop, no crash (gap lock-best-effort)."""

import unittest
from unittest import mock

from herdr_bartender import background
from herdr_bartender.cache import CacheReadError, LockTimeout
from herdr_bartender.schedule import CacheView, IDLE_EXIT_SECONDS
from herdr_bartender.snapshot import Instance, ProcessSnapshot
from tests.support import SandboxTestCase

BUSY_LOOP_GUARD = 50
PRESENT = ProcessSnapshot(bartender=Instance(4242, "1", True), herdr=Instance(4343, "1", True), herdr_alive=True)
ALL_GONE = ProcessSnapshot(bartender=Instance(None, None, True), herdr=Instance(None, None, True), herdr_alive=False)


def outcome(snapshot=PRESENT, sessions=0, healthy=True):
    return background.PassOutcome(snapshot, CacheView(sessions=sessions), healthy)


class ReconcilerLoopCase(SandboxTestCase):
    start_bridge = False

    def setUp(self):
        super().setUp()
        self.clock = self.use_fake_clock()
        self.pending = self.state_dir / "reconciler.pending"


class ReconcilerCacheFailureTests(ReconcilerLoopCase):

    def _failing_pass(self, error):
        calls = []

        def run(*_args):
            calls.append(self.clock.monotonic())
            if len(calls) > BUSY_LOOP_GUARD:
                raise AssertionError("busy loop: the pass was re-run without any backoff")
            raise error
        return calls, run

    def test_persistent_cache_error_backs_off_and_gives_up(self):
        """Finding (reconciler busy loop): a CacheError that persists (EACCES/EIO on active-sessions.json) is retried
        after a growing sleep, never immediately, and the run ends after a bounded number of failed passes with
        reconciler.pending left for the next run."""
        calls, run = self._failing_pass(CacheReadError("EACCES"))
        with mock.patch.object(background, "_sweep_pass", side_effect=run):
            background.run_reconcile_background()
        self.assertEqual(len(calls), background.CACHE_FAILURE_LIMIT)
        gaps = [later - earlier for earlier, later in zip(calls, calls[1:])]
        self.assertTrue(all(gap >= background.CACHE_RETRY_BASE_SECONDS for gap in gaps), gaps)
        self.assertEqual(gaps, sorted(gaps), "the backoff never shrinks")
        self.assertLessEqual(max(gaps), background.CACHE_RETRY_MAX_SECONDS)
        self.assertTrue(self.pending.exists(), "unfinished work stays flagged for the next reconciler run")

    def test_transient_cache_error_is_retried_after_a_backoff(self):
        """A contended lock in one pass is retried after a short sleep; a clean pass resets the failure count."""
        times = []

        def run(*_args):
            times.append(self.clock.monotonic())
            if len(times) == 1:
                raise LockTimeout("contended")
            (self.state_dir / "DISABLED").touch()  # end the run after the clean pass
            return outcome(sessions=1)

        with mock.patch.object(background, "_sweep_pass", side_effect=run):
            background.run_reconcile_background()
        self.assertEqual(len(times), 2)
        self.assertGreaterEqual(times[1] - times[0], background.CACHE_RETRY_BASE_SECONDS)

    def test_absent_bartender_export_survives_cache_error(self):
        """Finding (absent-export branch): a CacheError while exporting after >12h of Bartender absence ends the run
        cleanly instead of crashing the reconciler loop."""
        def gone(*_args):
            self.clock.advance(43_201)
            return outcome(ALL_GONE, sessions=1, healthy=False)

        with mock.patch.object(background, "_sweep_pass", side_effect=gone), \
                mock.patch.object(background, "_heartbeat_pass", side_effect=gone), \
                mock.patch.object(background, "all_sessions", side_effect=CacheReadError("EIO")), \
                mock.patch.object(background, "evict_exported_sessions") as export:
            background.run_reconcile_background()
        export.assert_not_called()
        self.assertTrue(self.pending.exists())


class LostWakeupTests(ReconcilerLoopCase):
    """Hand-off protocol: an event that flags reconciler.pending after the loop's final check, while
    reconciler.lock is still held, sees the singleton busy and spawns nothing; the exiting reconciler must."""

    def _exit_idle(self, flag_while_exiting):
        real_idle_expired = background.idle_expired

        def idle_expired(state, now):
            expired = real_idle_expired(state, now)
            if expired and flag_while_exiting:
                self.pending.touch()  # an event's touch_reconciler_pending() in the exit window
            return expired

        with mock.patch.object(background, "_sweep_pass", return_value=outcome()), \
                mock.patch.object(background, "idle_expired", side_effect=idle_expired):
            background.run_reconcile_background()

    def test_work_flagged_while_exiting_starts_a_successor(self):
        self._exit_idle(flag_while_exiting=True)
        self.assertEqual(len(self.spawner.calls), 1)
        self.assertEqual(self.spawner.in_critical_section, [False])

    def test_quiet_exit_spawns_nothing(self):
        start = self.clock.time()
        self._exit_idle(flag_while_exiting=False)
        self.assertEqual(self.spawner.calls, [])
        self.assertGreaterEqual(self.clock.time() - start, IDLE_EXIT_SECONDS, "idle exit only after 60s")


if __name__ == "__main__":
    unittest.main()
