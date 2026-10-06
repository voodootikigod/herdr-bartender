"""npx adversarial-review gate round 34: --cleanup never reads an unreadable cache as "nothing to clean" (R82)."""

import os
import time
import unittest
from unittest import mock

from herdr_bartender import cleanup
from herdr_bartender.cache import CacheError
from herdr_bartender.cleanup import EXIT_UNCONFIRMED, _Progress, _leftovers, run_cleanup
from tests.support import SandboxTestCase
from tests.support.lock_holder import hold_lock
from tests.support.reconciler_fixtures import seed, session


class UnreadableCacheTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        if os.geteuid() == 0:
            self.skipTest("root reads mode-000 files")
        self.sid_ = self.sid("w1:pUnread")
        seed(self.cache_mgr, {self.sid_: session("w1:pUnread", "Working", seq=2, now=time.time())})
        (self.state_dir / "DISABLED").touch()
        self.cache_file = self.cache_mgr.cache_file
        os.chmod(self.cache_file, 0)
        self.addCleanup(os.chmod, self.cache_file, 0o600)

    def test_unstageable_and_unreadable_cache_is_unconfirmed(self):
        hold_lock(self, self.cache_mgr.lock_file, seconds=30)
        with mock.patch.object(cleanup, "cleanup_budget", return_value=1.0):
            self.assertEqual(run_cleanup(bridge_url=self.mock_url), EXIT_UNCONFIRMED,
                             "exit 0 would let the rollback delete a cache it never read")

    def test_unreadable_cache_in_the_final_scan_leaves_targets_unconfirmed(self):
        progress = _Progress(targeted={self.sid_: 3})
        with mock.patch.object(cleanup, "_leftovers_locked", side_effect=CacheError("lock lost")):
            leftovers = _leftovers(self.cache_mgr, progress)
        self.assertTrue(leftovers, "nothing may count as confirmed from an unreadable cache")
        self.assertIn(self.sid_, leftovers.others)


if __name__ == "__main__":
    unittest.main()
