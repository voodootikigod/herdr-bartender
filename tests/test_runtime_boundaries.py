"""W1 refactor boundaries: no import-time I/O, no subprocess inside cache locks, guarded spawns."""

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from herdr_bartender import hooks, process, runtime, sender, watchdog
from herdr_bartender.reconciler import reconcile_active_sessions
from tests.support import SandboxTestCase


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
        """reconcile_active_sessions() resolves our `ps` start time before taking the cache lock."""
        seen = []

        def fake_start_time(pid=None):
            seen.append(runtime.IN_CRITICAL_SECTION)
            return "Sat Oct  4 09:00:00 2026"

        process.reset_caches()
        with mock.patch.object(process, "get_process_start_time", side_effect=fake_start_time):
            reconcile_active_sessions(self.state_dir)
        self.assertEqual(seen, [False], "start time must be looked up exactly once, outside the critical section")

    def test_watchdog_handoff_respects_unit_testing_guard(self):
        """The SIGALRM hand-off spawns via ensure_reconciler_running(), which is a no-op under unit tests."""
        self.assertTrue(os.environ.get("HERDR_BARTENDER_UNIT_TESTING"))
        (self.state_dir / "active-sessions.json").write_text(json.dumps(
            {"sessions": {self.sid("w1:pWatchdog"): {"seq": 2, "delivered_seq": 1}}}))
        runtime.IN_CRITICAL_SECTION = False
        with mock.patch.object(sender.subprocess, "Popen") as popen, \
                mock.patch.object(watchdog, "ensure_reconciler_running",
                                  wraps=sender.ensure_reconciler_running) as handoff:
            with self.assertRaises(SystemExit):
                watchdog._timeout_watchdog(None, None)
        handoff.assert_called_once()
        popen.assert_not_called()

    def test_mark_process_start_rebaselines_clock(self):
        """cli.main() re-baselines the deadline budget once imports are done (as the monolith did)."""
        runtime.START_TIME = -1000.0
        runtime.mark_process_start()
        self.assertGreater(runtime.time_remaining(), 0.1)
        self.assertGreater(runtime.PROCESS_ARRIVAL_TIME_NS, 0)


if __name__ == "__main__":
    unittest.main()
