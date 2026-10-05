"""Reconciler loop robustness (Plan §5.1 items 3, 5, 11): bounded pass rates, clock steps, horizons, markers.

Gaps (W4 review): self-inflicted pending touches and unconsumable envelopes must never turn the loop into a busy loop;
a backward wall-clock step must not stall a retry; the 12h terminal horizon counts Bartender's absence only (wake-ups
end the 300s backoff, not the horizon); the heartbeat keeps markers under 60s during long passes and never re-creates
a marker; the terminal export retries what it could not write before exiting.
"""

import json
import os
import time
import unittest
from unittest import mock

from herdr_bartender import background, reconciler
from herdr_bartender.background import run_reconcile_background
from herdr_bartender.orphans import _lock_path, pending_dir_for
from herdr_bartender.paths import get_orphan_path
from herdr_bartender.results import write_result_envelope
from herdr_bartender.delivery_state import Outcome, Transmission
from herdr_bartender.schedule import CacheView
from herdr_bartender.snapshot import Instance, ProcessSnapshot
from tests.support import SandboxTestCase
from tests.support.reconciler_fixtures import LoopRunner, pane_file, read_cache, seed, session

ABSENT_ALIVE = ProcessSnapshot(bartender=Instance(None, None, True), herdr=Instance(2, "1", True))
ABSENT_DEAD = ProcessSnapshot(bartender=Instance(None, None, True), herdr=Instance(None, None, True),
                              herdr_alive=False)


class LoopCase(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.clock = self.use_fake_clock()
        self.pending = self.state_dir / "reconciler.pending"

    def sessions(self):
        return read_cache(self.cache_mgr)["sessions"]

    def journal(self):
        pending = pending_dir_for(get_orphan_path())
        return sorted(pending.glob("*.json")) if pending.is_dir() else []

    def mocked_passes(self, snapshot_for, stop_after, sessions=1):
        """Run the loop with stubbed passes; ``snapshot_for(n)`` picks each pass's snapshot. Returns [(t, kind)]."""
        calls = []

        def record(kind):
            def run(*_args):
                calls.append((self.clock.time(), kind))
                if stop_after(calls):
                    (self.state_dir / "DISABLED").touch()
                return background.PassOutcome(snapshot_for(len(calls)), CacheView(sessions=sessions), None)
            return run

        with mock.patch.object(background, "_sweep_pass", side_effect=record("sweep")), \
                mock.patch.object(background, "_heartbeat_pass", side_effect=record("heartbeat")):
            run_reconcile_background(bridge_url=self.mock_url)
        return calls


class BoundedPassRateTests(LoopCase):
    def test_unopenable_orphan_lock_neither_busy_loops_nor_grows_the_journal(self):
        """A horizon export that can only be journaled (the orphan .lock is a directory: EISDIR) must not re-run the
        pass at once (the reconciler's own journal entry is no wake-up) nor journal the same export every pass."""
        sid = self.sid("w1:pHz")
        seed(self.cache_mgr, {sid: session("w1:pHz", "Ended", seq=3, delivered=False, now=self.clock.time(),
                                           delivery_status="non_retryable_failed",
                                           orphaned_at=self.clock.time() - 44000)})
        _lock_path(get_orphan_path()).mkdir()
        start = self.clock.time()
        runner = LoopRunner(self.state_dir, stop=lambda: self.clock.time() - start > 100, max_passes=40)
        runner.run(bridge_url=self.mock_url)
        self.assertLessEqual(len(runner.sweep_times()), 7, runner.sweep_times())
        self.assertEqual(len(self.journal()), 1, "one journaled export, not one per pass")
        self.assertIn(sid, self.sessions(), "kept until the export is really written")

    def test_long_dead_herdr_does_not_force_a_pass_every_half_second(self):
        """A past Herdr-dead expiry time (dead > 300s) is handled by the pass that crossed it; it must not keep the
        loop's next due time in the past (one full pass, with its pgrep/ps snapshot, every 0.5s)."""
        sid = self.sid("w1:pDeadLong")
        seed(self.cache_mgr, {sid: session("w1:pDeadLong", "Ended", seq=2, delivered=False, now=self.clock.time(),
                                           delivery_status="non_retryable_failed")},
             herdr_dead_since=self.clock.time() - 400)
        self.clear_fake_processes()
        self.add_fake_process("Bartender 6", pid=424200)
        start = self.clock.time()
        runner = LoopRunner(self.state_dir, stop=lambda: self.clock.time() - start > 100, max_passes=40)
        runner.run(bridge_url=self.mock_url)
        self.assertLessEqual(len(runner.sweep_times()), 7, runner.sweep_times())

    def test_directory_named_like_an_envelope_is_not_work(self):
        (self.state_dir / "spool" / "weird.json").mkdir(parents=True)
        start = self.clock.time()
        runner = LoopRunner(self.state_dir, max_passes=40)
        runner.run(bridge_url=self.mock_url)
        self.assertFalse((self.state_dir / "DISABLED").exists(), "idled out")
        self.assertLess(self.clock.time() - start, 61.0)
        log = (self.state_dir / "plugin.log").read_text() if (self.state_dir / "plugin.log").exists() else ""
        self.assertNotIn("cannot consume", log, "never even taken for an envelope (the consumers skip it too)")

    def test_a_pending_touch_on_every_pass_is_rate_limited(self):
        """Plan §5.1 item 3 re-runs a pass at once when work was flagged during it, but a flag raised on EVERY pass
        (whatever raises it) must not spin: consecutive immediate re-runs are 0.5s apart."""
        def stop(calls):
            self.pending.touch()
            return len(calls) > 60 or calls[-1][0] - calls[0][0] > 10

        calls = self.mocked_passes(lambda _n: ABSENT_ALIVE, stop)
        self.assertGreater(calls[-1][0] - calls[0][0], 10, f"{len(calls)} passes in no time")
        self.assertLessEqual(len(calls), 25)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    def test_unremovable_envelope_is_retried_on_the_cadence_not_in_a_busy_loop(self):
        tx = Transmission(self.sid("w1:pGone"), "w1:pGone", "Ended", 4)
        write_result_envelope(tx, Outcome("success"))
        results = self.state_dir / "results"
        results.chmod(0o500)
        self.addCleanup(results.chmod, 0o700)
        start = self.clock.time()
        runner = LoopRunner(self.state_dir, stop=lambda: self.clock.time() - start > 100, max_passes=40)
        runner.run(bridge_url=self.mock_url)
        self.assertTrue(any(results.glob("*.json")), "the envelope really could not be removed")
        self.assertLessEqual(len(runner.sweep_times()), 7, runner.sweep_times())


class ClockStepTests(LoopCase):
    def test_backward_wall_clock_step_does_not_stall_a_retry(self):
        sid = self.sid("w1:pStep")
        now = self.clock.time()
        seed(self.cache_mgr, {sid: session("w1:pStep", delivered=False, now=now, delivery_attempts=1,
                                           next_retry_at=now + 1.0)})
        self.clock.step_back(3600)
        start, sent_at = self.clock.time(), []
        self.bridge.on_post = lambda p: sent_at.append(self.clock.time()) if p.get("session_id") == sid else None
        LoopRunner(self.state_dir, stop=lambda: bool(sent_at) or self.clock.time() - start > 120).run(
            bridge_url=self.mock_url)
        self.assertTrue(sent_at, "the retry was sent")
        self.assertLess(sent_at[0] - start, 2.0)


class AbsenceCounterTests(LoopCase):
    def test_backoff_starts_right_after_1000s_of_absence(self):
        calls = self.mocked_passes(lambda _n: ABSENT_ALIVE, lambda c: c[-1][0] - c[0][0] > 1700)
        start = calls[0][0]
        sweeps = [round(t - start) for t, kind in calls if kind == "sweep"]
        self.assertIn(1000, sweeps)
        after = sweeps[sweeps.index(1000):][:4]
        self.assertEqual(after, [1000, 1020, 1320, 1620], sweeps)

    def test_pending_touch_returns_the_backoff_to_the_20s_cadence(self):
        touched = []

        def stop(calls):
            elapsed = calls[-1][0] - calls[0][0]
            if elapsed > 1500 and not touched:
                touched.append(calls[-1][0])
                self.pending.touch()
            return bool(touched) and calls[-1][0] - touched[0] > 100

        calls = self.mocked_passes(lambda _n: ABSENT_ALIVE, stop)
        after = [round(t - touched[0]) for t, kind in calls if kind == "sweep" and t > touched[0]]
        self.assertEqual(after[:4], [20, 40, 60, 80], "an immediate pass, then the 20s cadence again")

    def test_wake_ups_do_not_postpone_the_terminal_horizon(self):
        """The reconciler's own touches (and any wake-up) end the 300s backoff but never restart the 12h horizon."""
        sid = self.sid("w1:pTerm")
        seed(self.cache_mgr, {sid: session("w1:pTerm", now=self.clock.time())})

        def stop(calls):
            self.pending.touch()
            self.clock.advance(5000)
            return len(calls) > 20

        calls = self.mocked_passes(lambda _n: ABSENT_DEAD, stop)
        self.assertFalse((self.state_dir / "DISABLED").exists(), "exited at the terminal horizon")
        self.assertLess(len(calls), 15)
        self.assertNotIn(sid, self.sessions())


class TerminalExportRetryTests(LoopCase):
    def test_terminal_export_retries_a_failed_export_before_exiting(self):
        sid = self.sid("w1:pRetryExport")
        seed(self.cache_mgr, {sid: session("w1:pRetryExport", now=self.clock.time())})
        real, attempts = reconciler.export_orphan_record, []

        def flaky(*args, **kwargs):
            attempts.append(args[0])
            return False if len(attempts) == 1 else real(*args, **kwargs)

        with mock.patch.object(reconciler, "export_orphan_record", side_effect=flaky):
            self.mocked_passes(lambda _n: ABSENT_DEAD, lambda c: (self.clock.advance(5000), len(c) > 20)[1])
        self.assertFalse((self.state_dir / "DISABLED").exists())
        self.assertNotIn(sid, self.sessions())
        self.assertIn(sid, json.loads(get_orphan_path().read_text())["sessions"])


class HeartbeatMarkerTests(LoopCase):
    def _aged_marker(self, pane):
        marker = pane_file(self.state_dir, pane)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1")
        aged = time.time() - 90
        os.utime(marker, (aged, aged))
        return marker

    def test_heartbeat_never_creates_a_missing_marker(self):
        """Markers are created only by a confirmed delivery; the heartbeat only refreshes existing ones (a marker
        removed by a concurrent Ended must not come back and suppress the vendor hooks)."""
        seed(self.cache_mgr, {self.sid("w1:pNoMarker"): session("w1:pNoMarker", now=self.clock.time())})
        (self.state_dir / "panes").mkdir(parents=True, exist_ok=True)
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        self.assertFalse(pane_file(self.state_dir, "w1:pNoMarker").exists())

    def test_heartbeat_keeps_markers_fresh_during_a_long_pass(self):
        """Plan §5.1 item 5: markers stay under 60s old even when one pass takes 90s (six 15s POSTs)."""
        now = self.clock.time()
        seed(self.cache_mgr, {self.sid("w1:pHb"): session("w1:pHb", now=now)})
        self._aged_marker("w1:pHb")
        seed(self.cache_mgr, {self.sid(f"w1:pSlow{i}"): session(f"w1:pSlow{i}", delivered=False, now=now)
                              for i in range(6)})
        self.bridge.on_post = lambda _p: self.clock.advance(15.0)
        refreshed, real = [], background.refresh_pane_marker

        def refresh(pane):
            if pane == "w1:pHb":
                refreshed.append(self.clock.time())
            return real(pane)

        start = self.clock.time()
        with mock.patch.object(background, "refresh_pane_marker", side_effect=refresh):
            run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        self.assertGreaterEqual(self.clock.time() - start, 90.0)
        stamps = [start] + refreshed + [self.clock.time()]
        self.assertLess(max(b - a for a, b in zip(stamps, stamps[1:])), 60.0, refreshed)


if __name__ == "__main__":
    unittest.main()
