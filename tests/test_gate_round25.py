"""npx adversarial-review gate round 25: a symlink at a marker path is never refreshed through, nor trusted by
the guard; any entry at a flag path fails open (R73)."""

import os
import time
import unittest

from herdr_bartender.markers import refresh_pane_marker
from tests.support import run_guard
from tests.test_hooks_guard import _GuardCase

PANE = "w1:pLink"


class SymlinkedMarkerTests(_GuardCase):
    def test_heartbeat_never_refreshes_through_a_symlink(self):
        marker, _ = self.paths(PANE)
        victim = self.tmp / "victim.txt"
        victim.write_text("x")
        os.utime(victim, (1_000_000, 1_000_000))
        marker.symlink_to(victim)
        self.assertFalse(refresh_pane_marker(PANE))
        self.assertEqual(int(victim.stat().st_mtime), 1_000_000, "the symlink target is never touched")
        self.assertFalse(os.path.lexists(marker), "the bogus marker is removed so the guard falls through")

    def test_guard_does_not_trust_a_symlinked_marker(self):
        script = self.script("guard-link.sh", 'echo "PASSTHROUGH"')
        marker, _ = self.paths(PANE)
        fresh = self.tmp / "fresh.txt"
        fresh.write_text(str(int(time.time())))
        marker.symlink_to(fresh)
        res = run_guard(script, "Working", env_extra={"HERDR_PANE_ID": PANE})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("PASSTHROUGH", res.stdout, "a symlinked marker is never proof of Herdr ownership")

    def test_dangling_symlink_flag_fails_open(self):
        script = self.script("guard-flag.sh", 'echo "PASSTHROUGH"')
        self.fresh_marker(PANE)
        control = run_guard(script, "Working", env_extra={"HERDR_PANE_ID": PANE})
        self.assertNotIn("PASSTHROUGH", control.stdout, "control: a healthy pane suppresses")
        (self.state_dir / "DISABLED").symlink_to(self.tmp / "nowhere")
        res = run_guard(script, "Working", env_extra={"HERDR_PANE_ID": PANE})
        self.assertIn("PASSTHROUGH", res.stdout, "any entry at DISABLED (even a dangling symlink) fails open")


if __name__ == "__main__":
    unittest.main()
