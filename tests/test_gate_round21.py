"""npx adversarial-review gate round 21: --cleanup's budget covers its lock waits; bounded hook rereads (R69)."""

import json
import time
import unittest
from pathlib import Path
from unittest import mock

from herdr_bartender import cleanup, hooks_fs
from herdr_bartender.cleanup import EXIT_UNCONFIRMED, run_cleanup
from herdr_bartender.paths import get_orphan_path
from tests.support import SandboxTestCase
from tests.support.lock_holder import hold_lock
from tests.support.reconciler_fixtures import seed, session


class CleanupBudgetCoversLockWaitsTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.sids = [self.sid(f"w1:pB{n}") for n in range(3)]
        seed(self.cache_mgr, {sid: session(f"w1:pB{n}", "Working", seq=2, now=time.time())
                              for n, sid in enumerate(self.sids)})
        (self.state_dir / "DISABLED").touch()

    def test_contended_cache_lock_ends_within_the_budget_with_exports(self):
        """R69: a cache lock held for longer than the whole budget neither overruns it nor aborts (exit 1):
        the run ends inside the budget, exports every session from a lock-free snapshot and exits 2."""
        hold_lock(self, self.cache_mgr.lock_file, seconds=30)
        started = time.monotonic()
        with mock.patch.object(cleanup, "cleanup_budget", return_value=1.0):
            rc = run_cleanup(bridge_url=self.mock_url)
        elapsed = time.monotonic() - started
        self.assertEqual(rc, EXIT_UNCONFIRMED)
        self.assertLess(elapsed, 3.0, "every cache lock wait is clamped to the remaining budget")
        exported = json.loads(get_orphan_path().read_text())["sessions"]
        self.assertEqual(sorted(exported), sorted(self.sids))
        self.assertEqual(self.bridge.requests, [], "nothing was staged, so nothing was sent")


class BoundedHookRereadTests(SandboxTestCase):
    def test_replace_rereads_the_hook_through_the_bounded_reader(self):
        """R69: the pre-replace re-check never loads an arbitrarily large hook whole."""
        hook = self.tmp / "claude-event-hook.sh"
        hook.write_bytes(b"#!/bin/bash\necho vendor\n")
        expected = hook.read_bytes()
        hook.write_bytes(b"#" * (3 * 1024 * 1024))   # replaced by a huge file meanwhile
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
            with self.assertRaises(OSError):
                hooks_fs.atomic_replace_hook(hook, b"#!/bin/bash\necho patched\n", 0o755, expected=expected)
        self.assertEqual(list(self.tmp.glob("claude-event-hook.sh.tmp.*")), [])


if __name__ == "__main__":
    unittest.main()
