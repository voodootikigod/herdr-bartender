"""npx adversarial-review gate round 26: the shell guard applies R63/R70's .vendor_active rules too (R74)."""

import json
import os
import unittest

from tests.support import run_guard
from tests.test_hooks_guard import _GuardCase

SID = "c91f0443-1e47-54f7-9c13-2c064cbee24f"


class GuardVendorActiveHardeningTests(_GuardCase):
    def setUp(self):
        super().setUp()
        self.set_herdr_dead()   # the guard falls through and records .vendor_active
        self.script_path = self.script("guard-va.sh", 'echo "PASSTHROUGH"')

    def run_event(self, pane, *args):
        res = run_guard(self.script_path, *args, env_extra={"HERDR_PANE_ID": pane})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("PASSTHROUGH", res.stdout)
        return res

    def test_symlinked_vendor_active_is_never_followed(self):
        pane = "w1:pVaLink"
        _, va = self.paths(pane)
        target = self.tmp / "crafted.json"
        target.write_text(json.dumps({"vendor_session_id": SID}))
        os.utime(target, (1_000_000, 1_000_000))
        va.symlink_to(target)
        self.run_event(pane, "Working")
        self.assertEqual(int(target.stat().st_mtime), 1_000_000, "the target is never touched")
        self.assertEqual(json.loads(target.read_text()), {"vendor_session_id": SID})
        self.assertFalse(va.is_symlink(), "the symlink is replaced by the guard's own regular record")

    def test_oversized_vendor_active_is_replaced_not_scanned(self):
        pane = "w1:pVaHuge"
        _, va = self.paths(pane)
        va.write_text(json.dumps({"vendor_session_id": SID}) + " " * 8192)
        self.run_event(pane, "Working")
        self.assertLessEqual(va.stat().st_size, 4096, "the oversized file was dropped and a fresh record written")

    def test_directory_at_vendor_active_blocks_writes(self):
        pane = "w1:pVaDir"
        _, va = self.paths(pane)
        va.mkdir()
        self.run_event(pane, json.dumps({"session_id": SID, "hook_event_name": "UserPromptSubmit"}))
        self.assertEqual(list(va.iterdir()), [], "nothing is moved into a directory planted at the path")


if __name__ == "__main__":
    unittest.main()
