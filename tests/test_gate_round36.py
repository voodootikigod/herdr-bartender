"""npx adversarial-review gate round 36: a journal or orphan-file rename that cannot be made durable is a failure,
never a silent success (R84)."""

import unittest
from unittest import mock

from herdr_bartender import orphans
from herdr_bartender.orphans import _lock_path, export_orphan_record, journal_orphan_exports
from herdr_bartender.paths import get_orphan_path
from tests.support import SandboxTestCase
from tests.support.lock_holder import hold_lock


DurabilityError = getattr(orphans, "DurabilityError", OSError)


class DirectoryFsyncFailureTests(SandboxTestCase):
    def test_fsync_directory_raises_instead_of_swallowing(self):
        with mock.patch.object(orphans.os, "fsync", side_effect=OSError(22, "Invalid argument")):
            with self.assertRaises(DurabilityError):
                orphans.fsync_directory(self.tmp)

    def test_undurable_journal_write_is_not_reported_durable(self):
        hold_lock(self, _lock_path(get_orphan_path()), seconds=30)   # forces the journal path
        sid = "herdr:h:w1:pFsync"
        with mock.patch.object(orphans, "fsync_directory", side_effect=DurabilityError(5, "EIO")):
            with self.assertRaises(OSError):
                journal_orphan_exports([(sid, {"pane_id": "w1:pFsync", "seq": 1})])
            self.assertFalse(export_orphan_record(sid, {"pane_id": "w1:pFsync", "seq": 1}))


if __name__ == "__main__":
    unittest.main()
