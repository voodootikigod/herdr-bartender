"""R38: an older reconciler left holding reconciler.lock after an in-place upgrade is detected and named.

Finding (operability): the monolith's reconciler takes the same ``reconciler.lock`` and only exits once no session
is live, so after ``git pull`` every new spawn deferred to it and the new reconciler code never ran - silently.
"""

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from herdr_bartender import background, reconciler_stamp
from herdr_bartender.handoff import RECONCILER_LOCK_NAME
from herdr_bartender.reconciler_stamp import (
    STAMP_FILE_NAME,
    STOP_COMMAND,
    clear_stamp,
    code_version,
    outdated_holder,
    write_stamp,
)
from tests.support import REPO_ROOT, SandboxTestCase
from tests.support.lock_holder import hold_lock


class StampCase(SandboxTestCase):
    start_bridge = False

    def setUp(self):
        super().setUp()
        self.clock = self.use_fake_clock()
        self.stamp = self.state_dir / STAMP_FILE_NAME
        self.lock = self.state_dir / RECONCILER_LOCK_NAME

    def stamp_as(self, **record):
        self.stamp.write_text(json.dumps({"version": code_version(), "pid": os.getpid(),
                                          "start_time": None, **record}))


class OutdatedHolderTests(StampCase):
    def test_holder_without_a_stamp_is_outdated(self):
        """The monolith never writes a stamp: a held lock without one is an older reconciler."""
        hold_lock(self, self.lock)
        self.assertIn("no version stamp", outdated_holder(self.state_dir))

    def test_other_version_or_dead_stamp_is_outdated(self):
        hold_lock(self, self.lock)
        self.stamp_as(version=1)
        self.assertIn("version stamp is 1", outdated_holder(self.state_dir))
        self.stamp_as(pid=99_999_997)
        self.assertIn("gone", outdated_holder(self.state_dir))
        self.stamp.write_text("{oops")
        self.assertIn("unreadable", outdated_holder(self.state_dir))

    def test_current_holder_or_no_holder_is_fine(self):
        self.stamp_as(pid=99_999_997)
        self.assertIsNone(outdated_holder(self.state_dir), "no lock held: nothing to warn about")
        fd = background._acquire_singleton(self.state_dir)
        self.addCleanup(background._release_singleton, fd)
        write_stamp(self.state_dir)
        self.assertIsNone(outdated_holder(self.state_dir))

    def test_a_reconciler_that_stamps_right_after_locking_is_not_flagged(self):
        """The settle re-check: the holder had just taken the lock and stamps within STAMP_SETTLE_SECONDS."""
        fd = background._acquire_singleton(self.state_dir)
        self.addCleanup(background._release_singleton, fd)
        with mock.patch.object(reconciler_stamp.clock, "sleep", side_effect=lambda _s: write_stamp(self.state_dir)):
            self.assertIsNone(outdated_holder(self.state_dir))

    def test_clear_leaves_another_holders_stamp(self):
        self.stamp_as(pid=os.getpid() + 1)
        clear_stamp(self.state_dir)
        self.assertTrue(self.stamp.exists())
        self.stamp_as()
        clear_stamp(self.state_dir)
        self.assertFalse(self.stamp.exists())


class CodeVersionTests(StampCase):
    """Round-2 low finding: the version was the constant 2, so an upgrade from one package release to the next left
    the previous release's reconciler holding the lock unflagged. It is now a digest of the package sources."""

    @contextlib.contextmanager
    def code_at(self, root):
        """This process's code version as if the package were installed at ``root`` (a fresh, unmemoised process)."""
        with mock.patch.object(reconciler_stamp, "CODE_ROOT", root), \
                mock.patch.object(reconciler_stamp, "_code_version", None):
            yield

    def test_version_is_the_schema_and_a_stable_source_digest(self):
        with self.code_at(reconciler_stamp.CODE_ROOT):   # fresh digest, not this test process's memo
            here = code_version()
        self.assertRegex(here, r"^2:[0-9a-f]{16}$")
        other = subprocess.run([sys.executable, "-c", "from herdr_bartender.reconciler_stamp import code_version; "
                                "print(code_version())"], cwd=str(REPO_ROOT), capture_output=True, text=True,
                               timeout=30, check=True).stdout.strip()
        self.assertEqual(other, here, "every process of the same code computes the same version")

    def test_a_package_upgrade_flags_the_running_reconciler(self):
        installed = self.tmp / "installed"
        shutil.copytree(reconciler_stamp.CODE_ROOT, installed, ignore=shutil.ignore_patterns("__pycache__"))
        hold_lock(self, self.lock)
        with self.code_at(installed):
            self.stamp_as(version=code_version())   # the running reconciler stamped the code it loaded
            self.assertIsNone(outdated_holder(self.state_dir))
        for changed in ("reconciler.py", "hook_guard.sh", "sender/step_c.py"):
            with self.subTest(changed=changed):
                target = installed / changed
                original = target.read_bytes()
                target.write_bytes(original + b"\n# next release\n")   # `git pull` in the symlinked checkout
                with self.code_at(installed):
                    self.assertRegex(outdated_holder(self.state_dir) or "", re.escape("its version stamp is '2:"))
                target.write_bytes(original)
        with self.code_at(installed):
            self.assertIsNone(outdated_holder(self.state_dir), "the same sources are the same version")


class ReconcilerStampsItselfTests(StampCase):
    def test_the_loop_runs_stamped_and_removes_the_stamp_on_exit(self):
        seen = []

        def run_loop(*_args):
            record = json.loads(self.stamp.read_text())
            seen.append((record["version"], record["pid"], self.stamp.stat().st_mode & 0o777))
            return False

        with mock.patch.object(background, "_run_loop", side_effect=run_loop):
            background.run_reconcile_background()
        self.assertEqual(seen, [(code_version(), os.getpid(), 0o600)])
        self.assertFalse(self.stamp.exists(), "removed before the lock is released")


class OperatorWarningTests(StampCase):
    def setUp(self):
        super().setUp()
        self.holder = hold_lock(self, self.lock)   # the old monolith's reconciler: holds the lock, never stamps

    def test_status_names_the_outdated_reconciler_and_the_stop_command(self):
        res = self.run_cli("--status")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("[WARNING] An older herdr-bartender reconciler holds reconciler.lock", res.stdout)
        self.assertIn(STOP_COMMAND, res.stdout)

    def test_no_warning_once_the_old_reconciler_is_gone(self):
        self.holder.release()
        res = self.run_cli("--status")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("older herdr-bartender reconciler", res.stdout)

    def test_startup_hook_logs_the_warning_and_still_spawns(self):
        res = self.run_cli("--reconcile-background")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(STOP_COMMAND, (self.state_dir / "plugin.log").read_text())
        self.assertEqual(len(self.subprocess_spawns()), 1)


if __name__ == "__main__":
    unittest.main()
