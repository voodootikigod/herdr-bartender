"""W1 refactor boundaries: no import-time I/O, no subprocess inside cache locks, guarded spawns."""

import ast
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from herdr_bartender import handoff, hooks, process, runtime, watchdog
from herdr_bartender.reconciler import reconcile_active_sessions
from tests.support import LAUNCHER, SandboxTestCase
from tests.support.lock_holder import hold_lock


class RuntimeBoundaryTests(SandboxTestCase):
    start_bridge = False

    def test_guard_template_is_not_read_at_import(self):
        """hooks.py must not read hook_guard.sh at import (event dispatch must not depend on it)."""
        self.assertNotIn("HOOK_GUARD_TEMPLATE", vars(hooks))
        self.assertTrue(hooks.HOOK_GUARD_TEMPLATE.startswith("# BEGIN HERDR-BARTENDER DEDUP GUARD"))
        self.assertFalse(hooks.HOOK_GUARD_TEMPLATE.endswith("\n"))

    def test_unreadable_guard_template_only_fails_install(self):
        """A missing hook_guard.sh makes install_hooks() return False and leaves vendor hooks untouched."""
        hooks_dir = hooks.get_vendor_hooks_dir()
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook = hooks_dir / "claude-event-hook.sh"
        original = "#!/bin/bash\nset -u\necho 'vendor hook'\n"
        hook.write_text(original)
        with mock.patch.object(hooks, "_GUARD_RESOURCE", Path(self.tmp) / "missing-guard.sh"), \
                mock.patch.object(hooks, "_guard_template_cache", None):
            self.assertIs(hooks.install_hooks(), False)
        self.assertEqual(hook.read_text(), original)

    def test_reconciler_resolves_start_time_outside_lock(self):
        """reconcile_active_sessions() resolves our `ps` start time and its whole process snapshot (Bartender,
        Herdr, previously recorded instances) before taking the cache lock (gap lock-discipline-subprocess)."""
        herdr_pid = process.get_herdr_pid()  # the recorded instances are probed by the snapshot (no restart here:
        with self.cache_mgr as data:         # this class has no mock bridge, so nothing may be sent)
            data["sessions"]["herdr:h:w1:p1"] = {"pane_id": "w1:p1", "desired_state": "Working", "seq": 1,
                                                 "delivered_seq": 1, "delivery_status": "delivered",
                                                 "last_event_at": time.time()}
            data["last_herdr_pid"] = herdr_pid
            data["last_herdr_start_time"] = process.get_process_start_time(herdr_pid)
            self.cache_mgr.save(data)
        seen = []
        real_run = process.subprocess.run

        def run(*args, **kwargs):
            seen.append(runtime.IN_CRITICAL_SECTION)
            return real_run(*args, **kwargs)

        process.reset_caches()
        with mock.patch.object(process.subprocess, "run", side_effect=run):
            reconcile_active_sessions(self.state_dir)
        self.assertTrue(seen, "the pass probes processes")
        self.assertNotIn(True, seen, "no ps/pgrep inside the cache critical section")

    def test_watchdog_handoff_goes_through_the_injected_spawner(self):
        """The SIGALRM hand-off (run outside the handler by run_bounded) spawns via the injectable handoff
        spawner (the sandbox records instead of starting a process); no Popen happens."""
        runtime.IN_CRITICAL_SECTION = False

        def expire():
            watchdog._timeout_watchdog(None, None)

        with mock.patch.object(handoff.subprocess, "Popen") as popen:
            self.assertEqual(watchdog.run_bounded(expire), 0)
        self.assertEqual(self.spawner.calls, [handoff.loop_argv()])
        popen.assert_not_called()

    def test_production_spawner_is_detached_and_singleton(self):
        """Plan §5.1 singleton: the production spawner starts `--reconcile-background --foreground` (the loop
        itself: the spawn already detaches) in a new session with no inherited stdio, and
        ensure_reconciler_running() does not spawn while reconciler.lock is held."""
        handoff.set_spawner(handoff.DetachedSpawner())
        with mock.patch.object(handoff.subprocess, "Popen") as popen:
            self.assertTrue(handoff.ensure_reconciler_running())
        hold_lock(self, self.state_dir / handoff.RECONCILER_LOCK_NAME)  # a running reconciler (another process)
        with mock.patch.object(handoff.subprocess, "Popen") as again:
            self.assertFalse(handoff.ensure_reconciler_running())
        again.assert_not_called()
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0][-2:], ["--reconcile-background", handoff.FOREGROUND_FLAG])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        for stream in ("stdin", "stdout", "stderr"):
            self.assertIs(popen.call_args.kwargs[stream], subprocess.DEVNULL)

    def test_disabled_never_spawns(self):
        (self.state_dir / "DISABLED").touch()
        self.assertFalse(handoff.ensure_reconciler_running())
        self.assertEqual(self.spawner.calls, [])

    def test_mark_process_start_rebaselines_clock(self):
        """Without a launcher baseline (in-process callers) cli.main() baselines the budget at its first statement."""
        runtime.START_TIME = -1000.0
        runtime.mark_process_start()
        self.assertGreater(runtime.time_remaining(), 0.1)
        self.assertGreater(runtime.PROCESS_ARRIVAL_TIME_NS, 0)

    def test_launcher_baseline_counts_package_import_against_the_deadline(self):
        """Plan §6.1 budget table ('process startup & module load' is inside the 1.5s) / gap watchdog-deadline-1p4:
        the launcher's pre-import baseline is the deadline origin and the arrival stamp, so the watchdog is armed
        for 1.5s minus the time spent importing the package."""
        runtime.PROCESS_DEADLINE_SECONDS = runtime.DEFAULT_DEADLINE_SECONDS
        launched_mono, launched_ns = time.monotonic() - 0.4, time.time_ns() - 400_000_000
        runtime.mark_process_start(launched_at=(launched_mono, launched_ns))
        self.assertEqual((runtime.START_TIME, runtime.PROCESS_ARRIVAL_TIME_NS), (launched_mono, launched_ns))
        self.assertAlmostEqual(runtime.PROCESS_ARRIVAL_TIME, launched_ns / 1e9, places=6)
        with mock.patch.object(watchdog.signal, "setitimer") as setitimer, \
                mock.patch.object(watchdog.signal, "signal"):
            self.assertTrue(watchdog.arm_watchdog())
        self.assertAlmostEqual(setitimer.call_args.args[1], 1.1, delta=0.05)

    def test_launcher_takes_the_baseline_before_importing_the_package(self):
        """Gap watchdog-deadline-1p4: bin/herdr-bartender captures the clock before importing herdr_bartender and
        hands it to main(launched_at=...)."""
        source = LAUNCHER.read_text()
        body = ast.parse(source).body

        def first(predicate):
            return next((i for i, node in enumerate(body) if predicate(node)), None)

        captured = first(lambda node: "time.monotonic()" in (ast.get_source_segment(source, node) or ""))
        imported = first(lambda node: isinstance(node, ast.ImportFrom) and (node.module or "").startswith("herdr_bartender"))
        self.assertIsNotNone(captured, "the launcher must capture time.monotonic()")
        self.assertIsNotNone(imported)
        self.assertLess(captured, imported)
        self.assertIn("main(launched_at=", source)


if __name__ == "__main__":
    unittest.main()
