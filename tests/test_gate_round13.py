"""Fixes for the npx adversarial-review findings on the committed branch (gate round 13).

1. --cleanup must not exit 0 while a confirmation lives only in results/ (R58).
2. rollback.sh must not `rm -rf` through a symlinked intermediate path component (R59).
3. touch_pane_failed must fail open when the .failed flag cannot be written (R60).
"""

import json
import os
import subprocess
import unittest
from unittest import mock

from herdr_bartender import cleanup
from herdr_bartender.cache import CacheError, LockTimeout
from herdr_bartender.cleanup import EXIT_OK, EXIT_UNCONFIRMED, run_cleanup
from herdr_bartender.markers import touch_pane_failed, touch_pane_marker
from herdr_bartender.paths import get_orphan_path, get_state_dir
from herdr_bartender.sanitize import get_hex_pane_id
from herdr_bartender.sender import step_c
from tests.support import REPO_ROOT, SandboxTestCase
from tests.support.guard_harness import make_shim, path_with
from tests.support.reconciler_fixtures import read_cache, seed, session


class CleanupDeferredConfirmationTests(SandboxTestCase):
    """R58: a Step C confirmation deferred to results/ is not a confirmed cleanup until it is in the cache."""

    def setUp(self):
        super().setUp()
        self.clock = self.use_fake_clock()
        now = self.clock.time()
        self.a, self.b = self.sid("w1:pD1"), self.sid("w1:pD2")
        seed(self.cache_mgr, {self.a: session("w1:pD1", "Working", seq=2, now=now),
                              self.b: session("w1:pD2", "Idle", seq=4, now=now)})
        (self.state_dir / "DISABLED").touch()

    def results_left(self):
        directory = self.state_dir / "results"
        return sorted(p.name for p in directory.glob("*.json")) if directory.is_dir() else []

    def test_deferred_confirmations_are_folded_into_the_cache_before_exit_0(self):
        with mock.patch.object(step_c, "_settle_locked", side_effect=LockTimeout("contended")):
            rc = run_cleanup(bridge_url=self.mock_url)
        self.assertEqual(rc, EXIT_OK)
        self.assertEqual(self.results_left(), [], "every deferred confirmation was applied before exit 0")
        self.assertEqual(read_cache(self.cache_mgr)["sessions"], {}, "confirmed Endeds are evicted durably")

    def test_unrecordable_deferred_confirmations_exit_2_with_orphans_exported(self):
        with mock.patch.object(step_c, "_settle_locked", side_effect=LockTimeout("contended")), \
                mock.patch.object(cleanup, "drain_results_dir", side_effect=CacheError("no lock"), create=True):
            rc = run_cleanup(bridge_url=self.mock_url)
        self.assertEqual(rc, EXIT_UNCONFIRMED, "a confirmation only in results/ must not report success")
        orphans = json.loads(get_orphan_path().read_text())["sessions"]
        self.assertEqual(sorted(orphans), sorted([self.a, self.b]))


class RollbackIntermediateSymlinkTests(SandboxTestCase):
    """R59: the state dir path must not traverse a symlink anywhere, not only at its last component."""
    start_bridge = True

    def test_symlinked_parent_is_never_removed(self):
        self.add_fake_process("Bartender 6", live=True)
        real_parent = self.tmp / "elsewhere"
        target = real_parent / "herdr-bartender"
        target.mkdir(parents=True)
        (target / "keep.txt").write_text("precious")
        via = self.tmp / "via"
        via.symlink_to(real_parent, target_is_directory=True)
        stub_bin = self.tmp / "rb-bin"
        make_shim(stub_bin, "pkill", "exit 1")
        env = {**os.environ, "PATH": path_with(stub_bin), "HERDR_PLUGIN_STATE_DIR": str(via / "herdr-bartender")}
        res = subprocess.run(["bash", str(REPO_ROOT / "scripts" / "rollback.sh")], capture_output=True, text=True,
                             env=env, cwd=str(self.tmp), stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("refusing to remove", res.stdout)
        self.assertEqual((target / "keep.txt").read_text(), "precious")
        self.assertTrue((target / "DISABLED").exists(), "the tombstone stays")


class FailedFlagFailOpenTests(SandboxTestCase):
    """R60: if <hex>.failed cannot be written, the healthy marker must still go so vendor hooks fall through."""

    def test_unwritable_failed_flag_still_removes_the_healthy_marker(self):
        pane = "w1:pF1"
        touch_pane_marker(pane)
        panes = get_state_dir() / "panes"
        hex_id = get_hex_pane_id(pane)
        (panes / f"{hex_id}.failed").mkdir()   # open(..., "w") on a directory raises
        touch_pane_failed(pane)
        self.assertFalse((panes / hex_id).exists(), "the guard must not see a healthy marker after a failure")


if __name__ == "__main__":
    unittest.main()
