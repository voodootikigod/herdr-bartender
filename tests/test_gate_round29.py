"""npx adversarial-review gate round 29: an owed orphan export is durable only when THIS record was exported,
not merely its (deterministic, reused) session id (R77)."""

import unittest
from unittest import mock

from herdr_bartender import reconciler
from herdr_bartender.cache import ORPHAN_MIRROR_OWED
from herdr_bartender.orphans import write_orphan_sessions
from herdr_bartender.paths import get_orphan_path
from herdr_bartender.reconciler import remirror_owed_sessions
from tests.support import SandboxTestCase
from tests.support.reconciler_fixtures import read_cache, seed


class StaleOrphanSidTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.sid_ = self.sid("w1:pStale")
        self.record = {"pane_id": "w1:pStale", "seq": 5, "desired_state": "Ended", "delivered_seq": 4,
                       "orphaned_ended": True, ORPHAN_MIRROR_OWED: True, "delivery_status": "non_retryable_failed"}
        seed(self.cache_mgr, {self.sid_: dict(self.record)})
        # An earlier turn of the same pane left an export under the same (deterministic) session id.
        write_orphan_sessions(get_orphan_path(), {self.sid_: {"pane_id": "w1:pStale", "seq": 1,
                                                              "desired_state": "Ended"}})

    def test_stale_same_sid_export_does_not_clear_the_owed_flag(self):
        with mock.patch.object(reconciler, "export_orphan_record", return_value=False):
            cleared = remirror_owed_sessions(self.cache_mgr, ((self.sid_, dict(self.record)),))
        self.assertEqual(cleared, ())
        self.assertTrue(read_cache(self.cache_mgr)["sessions"][self.sid_].get(ORPHAN_MIRROR_OWED),
                        "the current Ended is still owed: it stays out of the cap prune")

    def test_matching_export_clears_the_owed_flag(self):
        cleared = remirror_owed_sessions(self.cache_mgr, ((self.sid_, dict(self.record)),))
        self.assertEqual(cleared, (self.sid_,))
        self.assertNotIn(ORPHAN_MIRROR_OWED, read_cache(self.cache_mgr)["sessions"][self.sid_])


if __name__ == "__main__":
    unittest.main()
