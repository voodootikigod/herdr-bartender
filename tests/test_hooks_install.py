"""Vendor hook installer behavior (vendor hooks dir is sandboxed via HERDR_BARTENDER_VENDOR_HOOKS_DIR)."""

import os
import unittest

from herdr_bartender.hooks import HOOK_GUARD_TEMPLATE, install_hooks
from tests.support import SandboxTestCase


class HookInstallTests(SandboxTestCase):
    start_bridge = False

    # WEAK: t22-install-mode
    def test_p22_patched_hook_keeps_exec_bits(self):
        """Plan §10.1 #22: a patched hook keeps its executable mode bits."""
        test_hook = self.state_dir / "test-hook.sh"
        test_hook.write_text("#!/bin/bash\nset -u\necho 'vendor hook'\n")
        os.chmod(test_hook, 0o755)
        orig_mode = os.stat(test_hook).st_mode
        tmp_patched = test_hook.with_suffix(".tmp")
        tmp_patched.write_text(f"#!/bin/bash\nset -u\n{HOOK_GUARD_TEMPLATE}\necho 'vendor hook'\n")
        os.chmod(tmp_patched, orig_mode)
        os.replace(tmp_patched, test_hook)
        self.assertNotEqual(os.stat(test_hook).st_mode & 0o111, 0, "Patched hook must preserve executable mode bits")

    # WEAK: t68-hook-review
    # GAP hook-guard-installer/install-flag-unlink-order
    @unittest.expectedFailure
    def test_p68_install_hooks_clears_review_flags(self):
        """Plan §10.1 #68: an explicit install_hooks() unlinks HOOK_NEEDS_REVIEW and .hook_review_alerted."""
        hnr_test = self.state_dir / "HOOK_NEEDS_REVIEW"
        alerted_test = self.state_dir / ".hook_review_alerted"
        hnr_test.touch()
        alerted_test.touch()
        self.assertTrue(hnr_test.exists() and alerted_test.exists())

        install_hooks()
        self.assertFalse(hnr_test.exists(), "install_hooks must unlink HOOK_NEEDS_REVIEW")
        self.assertFalse(alerted_test.exists(), "install_hooks must unlink .hook_review_alerted")


if __name__ == "__main__":
    unittest.main()
