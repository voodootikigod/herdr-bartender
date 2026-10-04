"""Reconciler loop under cache failures: bounded backoff, no busy loop, no crash (gap lock-best-effort)."""

import unittest
from unittest import mock

from herdr_bartender import background
from herdr_bartender.cache import CacheReadError, LockTimeout
from tests.support import SandboxTestCase

BUSY_LOOP_GUARD = 50


class ReconcilerLoopCase(SandboxTestCase):
    start_bridge = False

    def setUp(self):
        super().setUp()
        self.clock = self.use_fake_clock()
        self.pending = self.state_dir / "reconciler.pending"
        hooks = mock.patch.object(background, "verify_vendor_hooks_intact", return_value=(True, []))
        hooks.start()
        self.addCleanup(hooks.stop)


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
        outcomes = [LockTimeout("contended"), (0, 0)]
        times = []

        def run(*_args):
            times.append(self.clock.monotonic())
            result = outcomes.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with mock.patch.object(background, "_sweep_pass", side_effect=run):
            background.run_reconcile_background()
        self.assertEqual(len(times), 2)
        self.assertGreaterEqual(times[1] - times[0], background.CACHE_RETRY_BASE_SECONDS)

    def test_absent_bartender_export_survives_cache_error(self):
        """Finding (absent-export branch): a CacheError while exporting after >12h of Bartender absence ends the run
        cleanly instead of crashing the reconciler loop."""
        pids = iter([None, None])

        def bartender_pid():
            pid = next(pids, None)
            self.clock.advance(43_201)
            return pid

        broken = mock.MagicMock()
        broken.__enter__.side_effect = CacheReadError("EIO")
        with mock.patch.object(background, "_sweep_pass", return_value=(0, 1)), \
                mock.patch.object(background, "get_bartender_pid", side_effect=bartender_pid), \
                mock.patch.object(background, "is_herdr_alive", return_value=False), \
                mock.patch.object(background, "BoundedSessionCache", return_value=broken), \
                mock.patch.object(background, "export_orphan_record") as export:
            background.run_reconcile_background()
        export.assert_not_called()
        self.assertTrue(self.pending.exists())


class LostWakeupTests(ReconcilerLoopCase):
    """Hand-off protocol: an event that flags reconciler.pending after the loop's final check, while
    reconciler.lock is still held, sees the singleton busy and spawns nothing; the exiting reconciler must."""

    def _exit_idle(self, flag_while_exiting):
        def delivery_down():
            if flag_while_exiting:
                self.pending.touch()  # an event's touch_reconciler_pending() in the exit window
            return False

        with mock.patch.object(background, "_sweep_pass", return_value=(0, 0)), \
                mock.patch.object(background, "is_delivery_down", side_effect=delivery_down):
            background.run_reconcile_background()

    def test_work_flagged_while_exiting_starts_a_successor(self):
        self._exit_idle(flag_while_exiting=True)
        self.assertEqual(len(self.spawner.calls), 1)
        self.assertEqual(self.spawner.in_critical_section, [False])

    def test_quiet_exit_spawns_nothing(self):
        self._exit_idle(flag_while_exiting=False)
        self.assertEqual(self.spawner.calls, [])


if __name__ == "__main__":
    unittest.main()
