"""bin/herdr-bartender launcher smoke tests (subprocess, sandboxed)."""

import json
import os
import subprocess
import sys
import unittest

from tests.support import LAUNCHER, SandboxTestCase


class LauncherTests(SandboxTestCase):
    start_bridge = False

    def test_launcher_is_executable(self):
        """The launcher stays an executable python3 script."""
        self.assertTrue(os.access(LAUNCHER, os.X_OK))
        self.assertTrue(LAUNCHER.read_text().startswith("#!/usr/bin/env python3"))

    def test_launcher_resolves_through_symlink(self):
        """A symlinked launcher (as in ~/.config/herdr/plugins) still finds the package."""
        plugin_dir = self.home / ".config" / "herdr" / "plugins" / "local"
        plugin_dir.mkdir(parents=True)
        link = plugin_dir / "herdr-bartender"
        link.symlink_to(LAUNCHER)
        res = subprocess.run([sys.executable, str(link), "--sessions"], capture_output=True, text=True,
                             env=dict(os.environ), timeout=15)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout)["sessions"], {})
        self.assertTrue((self.state_dir / "active-sessions.lock").exists(), "CLI must use the sandboxed state dir")

    def test_disabled_event_dispatch_is_noop(self):
        """Plan §10.1 #14 (CLI path): with DISABLED present, an event invocation exits 0 without touching the cache."""
        (self.state_dir / "DISABLED").touch()
        res = self.run_cli(env={"HERDR_PLUGIN_EVENT": "pane.closed",
                                "HERDR_PLUGIN_EVENT_JSON": json.dumps({"data": {"pane_id": "w1:p1"}})})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse((self.state_dir / "active-sessions.json").exists())

    def test_replay_orphans_usage(self):
        """--replay-orphans without a path prints usage and exits 1."""
        res = self.run_cli("--replay-orphans")
        self.assertEqual(res.returncode, 1)
        self.assertIn("Usage: --replay-orphans", res.stdout)


if __name__ == "__main__":
    unittest.main()
