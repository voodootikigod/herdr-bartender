"""Process deadline watchdog: R23 flag-only handler, deferred exit, 1.5s ITIMER_REAL, process-level bounds."""

import json
import os
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

from herdr_bartender import handoff, runtime, watchdog
from herdr_bartender.watchdog import WatchdogExpired
from tests.support import REPO_ROOT, SandboxTestCase

SLACK_SECONDS = 0.35  # interpreter start-up + scheduling only: package import counts inside the 1.5s deadline

SNIPPET = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
from herdr_bartender import runtime, watchdog
from herdr_bartender.cache import BoundedSessionCache
from herdr_bartender.paths import get_state_dir

runtime.mark_process_start()
runtime.PROCESS_DEADLINE_SECONDS = float(sys.argv[2])
watchdog.arm_watchdog()

def blocked():
    time.sleep(10)
    return 3

def critical():
    cache = BoundedSessionCache(get_state_dir())
    with cache as data:
        data["sessions"]["before"] = {"seq": 1}
        time.sleep(float(sys.argv[2]) + 0.4)
        data["sessions"]["after"] = {"seq": 2}
        cache.save(data)
    return 4

sys.exit(watchdog.run_bounded(blocked if sys.argv[3] == "blocked" else critical))
"""


class HandlerTests(SandboxTestCase):
    start_bridge = False

    def test_handler_outside_critical_section_raises_without_io(self):
        """R23 / Plan §6.1 (gap watchdog-handler-io): outside a critical section the handler sets the flag and
        raises WatchdogExpired; it opens no file, logs nothing and spawns nothing."""
        before = sorted(p.name for p in self.state_dir.iterdir())
        with mock.patch("builtins.open", side_effect=AssertionError("file I/O in handler")), \
                mock.patch.object(os, "open", side_effect=AssertionError("os.open in handler")), \
                mock.patch.object(handoff, "touch_reconciler_pending") as touch:
            with self.assertRaises(WatchdogExpired):
                watchdog._timeout_watchdog(signal.SIGALRM, None)
        self.assertTrue(runtime.PENDING_WATCHDOG_EXIT)
        self.assertEqual(self.spawner.calls, [], "the handler spawns nothing")
        touch.assert_not_called()
        self.assertEqual(sorted(p.name for p in self.state_dir.iterdir()), before)

    def test_handler_inside_critical_section_only_sets_flag(self):
        """Plan §6.1 L727 (gap t12-quarantine-signal): inside a critical section the handler defers (no exit)."""
        runtime.IN_CRITICAL_SECTION = True
        try:
            self.assertIsNone(watchdog._timeout_watchdog(signal.SIGALRM, None))
        finally:
            runtime.IN_CRITICAL_SECTION = False
        self.assertTrue(runtime.PENDING_WATCHDOG_EXIT)

    def test_handler_inside_deferred_section_only_sets_flag(self):
        """R23 + Plan §4.3 contention rule (finding: watchdog during the defer path): inside a deferred-exit section
        (lock wait, spool/result write) the handler only sets the flag; leaving the section hands off and exits 0."""
        with self.assertRaises(SystemExit) as cm:
            with watchdog.deferred_exit():
                self.assertIsNone(watchdog._timeout_watchdog(signal.SIGALRM, None))
                self.assertTrue(runtime.PENDING_WATCHDOG_EXIT)
                watchdog.honor_pending_exit()  # a cache release inside the section must not exit early
                self.assertEqual(self.spawner.calls, [])
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(self.spawner.calls, [handoff.reconciler_argv()])
        self.assertTrue((self.state_dir / "reconciler.pending").exists())
        self.assertFalse(runtime.IN_DEFER_SECTION)

    def test_deferred_section_without_deadline_is_transparent(self):
        """A deferred-exit section with no pending deadline neither exits nor masks exceptions."""
        with watchdog.deferred_exit():
            pass
        with self.assertRaises(KeyError):
            with watchdog.deferred_exit():
                runtime.PENDING_WATCHDOG_EXIT = True
                raise KeyError("propagates unchanged")
        self.assertFalse(runtime.IN_DEFER_SECTION)

    def test_run_bounded_hands_off_once_and_exits_zero(self):
        """Plan §6.1 L729 (gap watchdog-handler-io): the non-critical exit path hands off outside the handler."""
        def expire():
            raise WatchdogExpired()

        self.assertEqual(watchdog.run_bounded(expire), 0)
        self.assertEqual(self.spawner.calls, [handoff.reconciler_argv()])
        self.assertTrue((self.state_dir / "reconciler.pending").exists())

    def test_watchdog_armed_at_1p5s_from_process_start(self):
        """Plan §6.1 L698 (gap watchdog-deadline-1p4): ITIMER_REAL is 1.5s measured from the start baseline."""
        self.assertEqual(runtime.DEFAULT_DEADLINE_SECONDS, 1.5)
        runtime.PROCESS_DEADLINE_SECONDS = runtime.DEFAULT_DEADLINE_SECONDS
        runtime.START_TIME = time.monotonic() - 0.25
        with mock.patch.object(watchdog.signal, "setitimer") as setitimer, \
                mock.patch.object(watchdog.signal, "signal") as install:
            self.assertTrue(watchdog.arm_watchdog())
        install.assert_called_once_with(signal.SIGALRM, watchdog._timeout_watchdog)
        which, seconds = setitimer.call_args.args
        self.assertEqual(which, signal.ITIMER_REAL)
        self.assertAlmostEqual(seconds, 1.25, delta=0.05)


class BudgetFormulaTests(SandboxTestCase):
    start_bridge = False

    def test_socket_and_lock_budgets_follow_remaining_time(self):
        """Plan §6.1 L700/L704 (gap test-watchdog-tmp-orphanlock): with the 1.5s deadline the socket timeout is
        min(0.2, max(0.05, remaining - 0.3)) and the lock deadline min(0.2, max(0.02, remaining - 0.3)); at or
        below 0.3s remaining the shared budget gate refuses further network work."""
        from herdr_bartender import bridge, cache
        runtime.set_deadline_mode(runtime.DEADLINE_BOUNDED)
        runtime.PROCESS_DEADLINE_SECONDS = runtime.DEFAULT_DEADLINE_SECONDS
        rows = [(1.0, 0.2, 0.2, True), (0.45, 0.15, 0.15, True), (0.35, 0.05, 0.05, True), (0.25, 0.05, 0.02, False)]
        for remaining, sock, lock, allowed in rows:
            with self.subTest(remaining=remaining):
                runtime.START_TIME = time.monotonic() - (runtime.PROCESS_DEADLINE_SECONDS - remaining)
                self.assertAlmostEqual(bridge.socket_timeout(0.2), sock, delta=0.01)
                self.assertAlmostEqual(cache.lock_timeout_seconds(), lock, delta=0.01)
                self.assertEqual(runtime.budget_allows(0.3), allowed)


class ProcessLevelWatchdogTests(SandboxTestCase):
    start_bridge = False

    def _run_snippet(self, mode, deadline=0.4):
        t0 = time.monotonic()
        proc = subprocess.run([sys.executable, "-c", SNIPPET, str(REPO_ROOT), str(deadline), mode],
                              capture_output=True, text=True, timeout=20, env=dict(os.environ))
        return proc, time.monotonic() - t0

    def test_deadline_outside_critical_section_exits_zero(self):
        """Plan §6.1 / §10.1 #13: a real SIGALRM unwinds a blocked main flow; exit 0 with the reconciler hand-off."""
        proc, elapsed = self._run_snippet("blocked")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 0.4 + SLACK_SECONDS)
        self.assertTrue((self.state_dir / "reconciler.pending").exists())

    def test_deadline_inside_critical_section_is_deferred_until_unlock(self):
        """Plan §6.1 L728 / §10.1 #12: SIGALRM during the critical section waits for save + unlock, then exits 0;
        the cache is never left half-written."""
        proc, _ = self._run_snippet("critical")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sessions = json.loads((self.state_dir / "active-sessions.json").read_text())["sessions"]
        self.assertEqual(set(sessions), {"before", "after"})
        self.assertTrue((self.state_dir / "reconciler.pending").exists())


class ProcessDeadlineTests(SandboxTestCase):
    def test_p13_event_process_exits_within_deadline_against_slow_bridge(self):
        """Plan §10.1 #13 (gap t13-process-deadline): bin/herdr-bartender against a bridge that delays 3s exits 0
        within 1.5s (+ start-up slack) and leaves a consistent, undelivered cache handed to the reconciler."""
        self.bridge.delay = 3.0
        envelope = {"event": "pane.agent_status_changed",
                    "data": {"pane_id": "w1:pSlow", "workspace_id": "w1", "agent_status": "working", "agent": "claude"},
                    "context": {}}
        t0 = time.monotonic()
        proc = self.run_cli("pane.agent_status_changed", input=json.dumps(envelope))
        elapsed = time.monotonic() - t0
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, runtime.DEFAULT_DEADLINE_SECONDS + SLACK_SECONDS)
        with self.cache_mgr as data:
            record = data["sessions"][self.sid("w1:pSlow")]
        self.assertLess(record.get("delivered_seq", 0), record["seq"])
        self.assertNotEqual(record["delivery_status"], "delivered")
        self.assertTrue((self.state_dir / "reconciler.pending").exists(),
                        "undelivered work must be durably handed to the reconciler")


if __name__ == "__main__":
    unittest.main()
