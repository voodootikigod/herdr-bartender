"""npx adversarial-review gate round 24: rollback removes only a state dir the plugin created; --cleanup's orphan
journal flushes stay inside its budget (R72)."""

import json
import os
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from herdr_bartender import cleanup
from herdr_bartender.cleanup import run_cleanup
from herdr_bartender.orphans import _lock_path, export_orphan_record, pending_dir_for
from herdr_bartender.paths import OWNERSHIP_MARKER, get_orphan_path, get_state_dir
from tests.support import REPO_ROOT, SandboxTestCase
from tests.support.guard_harness import make_shim, path_with
from tests.support.lock_holder import hold_lock

ROLLBACK = REPO_ROOT / "scripts" / "rollback.sh"


class OwnershipMarkerTests(SandboxTestCase):
    def test_marker_written_only_into_a_directory_the_plugin_creates(self):
        fresh = self.tmp / "fresh" / "herdr-bartender"
        existing = self.tmp / "existing" / "herdr-bartender"
        existing.mkdir(parents=True)
        for path, owned in ((fresh, True), (existing, False)):
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": str(path)}):
                get_state_dir()
            self.assertEqual((path / OWNERSHIP_MARKER).is_file(), owned, path)


class RollbackOwnershipTests(SandboxTestCase):
    start_bridge = True

    def rollback(self, state_dir: Path):
        stub_bin = self.tmp / "rb-bin"
        make_shim(stub_bin, "pkill", "exit 1")
        env = {**os.environ, "PATH": path_with(stub_bin), "HERDR_PLUGIN_STATE_DIR": str(state_dir)}
        return subprocess.run(["bash", str(ROLLBACK)], capture_output=True, text=True, env=env, cwd=str(self.tmp),
                              stdin=subprocess.DEVNULL, timeout=60)

    def test_preexisting_override_dir_named_herdr_bartender_is_kept(self):
        """R72: a custom/shared override that already existed is never removed, whatever its name."""
        self.add_fake_process("Bartender 6", live=True)
        shared = self.tmp / "shared" / "herdr-bartender"
        shared.mkdir(parents=True)
        (shared / "keep.txt").write_text("precious")
        res = self.rollback(shared)
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("refusing to remove", res.stdout)
        self.assertEqual((shared / "keep.txt").read_text(), "precious")

    def test_override_dir_the_plugin_created_is_removed(self):
        self.add_fake_process("Bartender 6", live=True)
        owned = self.tmp / "own" / "herdr-bartender"
        with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": str(owned)}):
            get_state_dir()
        res = self.rollback(owned)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertFalse(owned.exists())


class CleanupJournalFlushBudgetTests(SandboxTestCase):
    def test_contended_orphan_lock_does_not_overrun_the_budget(self):
        """R72: the journal flushes wait only for what is left of the cleanup budget, not a fixed 5s each."""
        orphan = get_orphan_path()
        hold_lock(self, _lock_path(orphan), seconds=30)
        self.assertFalse(export_orphan_record("herdr:h:w1:pJ", {"pane_id": "w1:pJ", "desired_state": "Ended"}))
        self.assertTrue(list(pending_dir_for(orphan).glob("*.json")), "control: a journal entry is waiting")
        (self.state_dir / "DISABLED").touch()
        started = time.monotonic()
        with mock.patch.object(cleanup, "cleanup_budget", return_value=1.0):
            run_cleanup(bridge_url=self.mock_url)
        self.assertLess(time.monotonic() - started, 3.0)


if __name__ == "__main__":
    unittest.main()
