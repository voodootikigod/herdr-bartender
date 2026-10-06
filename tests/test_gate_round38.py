"""npx adversarial-review gate round 38: without an atomic exchange, hook replacement claims and links instead of
overwriting, so a concurrent vendor update is never lost (R86)."""

import os
import stat
import unittest
from unittest import mock

from herdr_bartender import hooks_fs
from herdr_bartender.boundedio import read_regular_file as real_read
from tests.support import SandboxTestCase

ORIGINAL = b"#!/bin/bash\necho vendor v1\n"
UPDATE = b"#!/bin/bash\necho vendor v2\n"
PATCHED = b"#!/bin/bash\necho patched\n"


class FallbackReplaceTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.hook = self.tmp / "claude-event-hook.sh"
        self.hook.write_bytes(ORIGINAL)
        patcher = mock.patch.object(hooks_fs, "exchange", return_value=False)   # a filesystem without exchange
        patcher.start()
        self.addCleanup(patcher.stop)

    def leftovers(self):
        return sorted(p.name for p in self.tmp.glob(".claude-event-hook.sh.claim.*")) + \
            sorted(p.name for p in self.tmp.glob("claude-event-hook.sh.tmp.*"))

    def replace_with_vendor_update_on_read(self, call_index):
        calls = []

        def read(path, *args, **kwargs):
            data = real_read(path, *args, **kwargs)
            calls.append(path)
            if len(calls) == call_index:
                self.hook.write_bytes(UPDATE)   # the vendor's updater lands right here
            return data

        with mock.patch.object(hooks_fs, "read_regular_file", side_effect=read):
            with self.assertRaises(hooks_fs.HookWriteError):
                hooks_fs.atomic_replace_hook(self.hook, PATCHED, 0o755, expected=ORIGINAL)

    def test_plain_fallback_replace(self):
        hooks_fs.atomic_replace_hook(self.hook, PATCHED, 0o750, expected=ORIGINAL)
        self.assertEqual(self.hook.read_bytes(), PATCHED)
        self.assertEqual(stat.S_IMODE(os.stat(self.hook).st_mode), 0o750)
        self.assertEqual(self.leftovers(), [])

    def test_update_after_the_check_is_put_back(self):
        self.replace_with_vendor_update_on_read(1)
        self.assertEqual(self.hook.read_bytes(), UPDATE)
        self.assertEqual(self.leftovers(), [])

    def test_update_while_the_path_is_claimed_is_kept(self):
        self.replace_with_vendor_update_on_read(2)
        self.assertEqual(self.hook.read_bytes(), UPDATE)
        self.assertEqual(self.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
