"""Unit tests for first-party agy-notify-hook.sh installer, uninstaller, and health/config audit."""

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from herdr_bartender import agy_hook, hooks, hooks_fs, hooks_integrity
from herdr_bartender.hooks import (
    AGY_HOOK_NAME,
    HOOK_NAMES,
    STATUS_INTACT,
    agy_source_path,
    audit_agy_hooks_config,
    check_hook_integrity,
    default_agy_hooks_config_path,
    install_agy_hook,
    install_hooks,
    uninstall_agy_hook,
    uninstall_hooks,
    verify_agy_hook_intact,
)
from tests.support import SandboxTestCase
from tests.support.hook_fixtures import (
    CLAUDE_HOOK,
    CODEX_HOOK,
    VENDOR_CLAUDE,
    VENDOR_CODEX,
    call_quietly,
    guard_count,
    mode_of,
    read_allowlist,
    seed_hook,
    seed_vendor_hooks,
    sha256,
    tmp_leftovers,
)

STUB_51_BYTES = b'printf \'{"decision":"allow"}\\n\'; exit 0\n'


class AgyHookInstallTests(SandboxTestCase):
    start_bridge = False

    def setUp(self):
        super().setUp()
        self.hooks_dir = self.vendor_hooks_dir
        self.source_bytes = agy_source_path().read_bytes()

    def test_vendor_hook_names_invariant_preserved(self):
        self.assertEqual(HOOK_NAMES, (CLAUDE_HOOK, CODEX_HOOK))
        self.assertNotIn(AGY_HOOK_NAME, HOOK_NAMES)

    def test_install_agy_hook_into_empty_dir_and_idempotent(self):
        self.hooks_dir.mkdir(parents=True, exist_ok=True)
        ok, out = call_quietly(install_agy_hook, self.hooks_dir)
        self.assertTrue(ok, out)
        self.assertIn(f"[+] Installed Antigravity hook in {AGY_HOOK_NAME}", out)

        target = self.hooks_dir / AGY_HOOK_NAME
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), self.source_bytes)
        self.assertEqual(mode_of(target), 0o755)
        self.assertEqual(tmp_leftovers(self.hooks_dir), [])

        ok2, out2 = call_quietly(install_agy_hook, self.hooks_dir)
        self.assertTrue(ok2, out2)
        self.assertIn(f"[=] Antigravity hook already current in {AGY_HOOK_NAME}", out2)
        self.assertEqual(target.read_bytes(), self.source_bytes)
        self.assertEqual(tmp_leftovers(self.hooks_dir), [])

    def test_install_agy_hook_overwrites_stub_and_fixes_exec_bit(self):
        self.assertEqual(len(STUB_51_BYTES), 40)  # sanity check stub content; also test exact 51-byte stub
        stub_51 = b"#!/bin/sh\n" + b'printf \'{"decision":"allow"}\\n\'; exit 0\n\n'
        self.assertEqual(len(stub_51), 51)

        for stub in (STUB_51_BYTES, stub_51):
            with self.subTest(stub_len=len(stub)):
                target = seed_hook(self.hooks_dir, AGY_HOOK_NAME, stub, mode=0o644)
                ok, out = call_quietly(install_agy_hook, self.hooks_dir)
                self.assertTrue(ok, out)
                self.assertIn(f"[+] Installed Antigravity hook in {AGY_HOOK_NAME}", out)
                self.assertEqual(target.read_bytes(), self.source_bytes)
                self.assertEqual(mode_of(target), 0o755)
                self.assertEqual(tmp_leftovers(self.hooks_dir), [])

        # Current content lacking exec bit gets repaired in-place on re-install
        os.chmod(target, 0o644)
        ok, out = call_quietly(install_agy_hook, self.hooks_dir)
        self.assertTrue(ok, out)
        self.assertIn(f"[=] Antigravity hook already current in {AGY_HOOK_NAME}", out)
        self.assertTrue(mode_of(target) & 0o100)

    def test_install_agy_hook_missing_dir_and_symlink_and_tmp_cleanup(self):
        self.assertFalse(self.hooks_dir.exists())
        ok, out = call_quietly(install_agy_hook, self.hooks_dir)
        self.assertFalse(ok)
        self.assertFalse(self.hooks_dir.exists())

        self.hooks_dir.mkdir(parents=True)
        outside = self.tmp / "outside.sh"
        outside.write_bytes(b"#!/bin/bash\necho keep\n")
        target = self.hooks_dir / AGY_HOOK_NAME
        target.symlink_to(outside)
        ok, _ = call_quietly(install_agy_hook, self.hooks_dir)
        self.assertFalse(ok)
        self.assertEqual(outside.read_bytes(), b"#!/bin/bash\necho keep\n")
        target.unlink()

        with mock.patch.object(hooks_fs, "bash_syntax_ok", return_value=(False, "bad syntax")):
            ok, out = call_quietly(install_agy_hook, self.hooks_dir)
        self.assertFalse(ok)
        self.assertIn("Syntax validation failed", out)
        self.assertFalse(target.exists())
        self.assertEqual(tmp_leftovers(self.hooks_dir), [])
        self.assertEqual(list(self.hooks_dir.iterdir()), [])

    def test_install_hooks_deploys_agy_without_polluting_sha_allowlist_or_integrity(self):
        paths = seed_vendor_hooks(self.hooks_dir)
        seed_hook(self.hooks_dir, AGY_HOOK_NAME, STUB_51_BYTES, mode=0o755)

        ok, out = call_quietly(install_hooks)
        self.assertTrue(ok, out)

        self.assertEqual(guard_count(paths[CLAUDE_HOOK]), (1, 1))
        self.assertEqual(guard_count(paths[CODEX_HOOK]), (1, 1))
        agy_path = self.hooks_dir / AGY_HOOK_NAME
        self.assertTrue(agy_path.is_file())
        self.assertEqual(agy_path.read_bytes(), self.source_bytes)
        self.assertEqual(mode_of(agy_path), 0o755)

        # vendor-hook-sha.json and check_hook_integrity().hooks must ONLY track claude & codex
        self.assertEqual(
            read_allowlist(self.state_dir),
            {CLAUDE_HOOK: sha256(VENDOR_CLAUDE), CODEX_HOOK: sha256(VENDOR_CODEX)},
        )
        integrity = check_hook_integrity()
        self.assertEqual(integrity.status, STATUS_INTACT)
        self.assertEqual([h.name for h in integrity.hooks], [CLAUDE_HOOK, CODEX_HOOK])

    def test_install_hooks_updates_standalone_agy_stub_when_present(self):
        seed_hook(self.hooks_dir, AGY_HOOK_NAME, STUB_51_BYTES, mode=0o755)
        ok, out = call_quietly(install_hooks)
        self.assertTrue(ok, out)
        self.assertEqual((self.hooks_dir / AGY_HOOK_NAME).read_bytes(), self.source_bytes)
        self.assertFalse((self.state_dir / "vendor-hook-sha.json").exists())

    def test_uninstall_removes_matching_agy_hook_and_preserves_custom_script(self):
        paths = seed_vendor_hooks(self.hooks_dir)
        ok, out = call_quietly(install_hooks)
        self.assertTrue(ok, out)
        agy_path = self.hooks_dir / AGY_HOOK_NAME
        self.assertTrue(agy_path.exists())

        ok, out = call_quietly(uninstall_hooks)
        self.assertTrue(ok, out)
        self.assertFalse(agy_path.exists())
        self.assertIn(f"[+] Uninstalled Antigravity hook {AGY_HOOK_NAME}", out)
        self.assertEqual(paths[CLAUDE_HOOK].read_bytes(), VENDOR_CLAUDE)
        self.assertEqual(paths[CODEX_HOOK].read_bytes(), VENDOR_CODEX)

        # Older version containing HERDR_PLUGIN_STATE_DIR is also uninstalled
        seed_hook(self.hooks_dir, AGY_HOOK_NAME, b'#!/bin/bash\nSTATE="${HERDR_PLUGIN_STATE_DIR:-}"\n')
        ok, out = call_quietly(uninstall_agy_hook, self.hooks_dir)
        self.assertTrue(ok, out)
        self.assertFalse(agy_path.exists())

        # Custom user script without HERDR_PLUGIN_STATE_DIR is preserved
        custom = b"#!/bin/bash\necho 'user custom antigravity hook'\n"
        seed_hook(self.hooks_dir, AGY_HOOK_NAME, custom, mode=0o755)
        ok, out = call_quietly(uninstall_hooks)
        self.assertTrue(ok, out)
        self.assertIn(f"[=] Keeping custom {AGY_HOOK_NAME}", out)
        self.assertTrue(agy_path.is_file())
        self.assertEqual(agy_path.read_bytes(), custom)

    def test_verify_agy_hook_intact_states(self):
        self.assertFalse(self.hooks_dir.exists())
        self.assertEqual(verify_agy_hook_intact(self.hooks_dir), (False, "hooks_dir_missing"))

        self.hooks_dir.mkdir(parents=True)
        self.assertEqual(verify_agy_hook_intact(self.hooks_dir), (False, "missing"))

        outside = self.tmp / "outside-agy.sh"
        outside.write_bytes(self.source_bytes)
        os.chmod(outside, 0o755)
        target = self.hooks_dir / AGY_HOOK_NAME
        target.symlink_to(outside)
        self.assertEqual(verify_agy_hook_intact(self.hooks_dir), (False, "symlink"))
        target.unlink()

        seed_hook(self.hooks_dir, AGY_HOOK_NAME, STUB_51_BYTES, mode=0o755)
        self.assertEqual(verify_agy_hook_intact(self.hooks_dir), (False, "outdated_or_stub"))

        seed_hook(self.hooks_dir, AGY_HOOK_NAME, self.source_bytes, mode=0o644)
        self.assertEqual(verify_agy_hook_intact(self.hooks_dir), (False, "not_executable"))

        os.chmod(target, 0o755)
        self.assertEqual(verify_agy_hook_intact(self.hooks_dir), (True, "intact"))


class AgyHooksConfigAuditTests(SandboxTestCase):
    start_bridge = False

    def test_default_path_resolves_under_sandbox_home(self):
        expected = Path(self.home) / ("." + "gemini") / "config" / "hooks.json"
        self.assertEqual(default_agy_hooks_config_path(), expected)
        self.assertEqual(audit_agy_hooks_config(), ())

    def test_audit_detects_top_level_and_nested_pretooluse_and_keeps_file_untouched(self):
        cfg = self.tmp / "hooks.json"

        # Clean configuration with PreInvocation, PostInvocation, Stop, SessionEnd
        clean_obj = {
            "hooks": {
                "PreInvocation": [{"hooks": [{"type": "command", "command": "/x/agy-notify-hook.sh"}]}],
                "PostInvocation": [{"hooks": [{"type": "command", "command": "/x/agy-notify-hook.sh"}]}],
                "Stop": [{"hooks": [{"type": "command", "command": "/x/agy-notify-hook.sh"}]}],
                "SessionEnd": [{"hooks": [{"type": "command", "command": "/x/agy-notify-hook.sh"}]}],
                "PreToolUse": [{"hooks": [{"type": "command", "command": "/x/other-security-guard.sh"}]}],
            }
        }
        cfg.write_text(json.dumps(clean_obj), encoding="utf-8")
        self.assertEqual(audit_agy_hooks_config(cfg), ())

        # Top-level {"hooks": {"PreToolUse": [...]}} shape
        top_shape = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": ".*",
                        "hooks": [{"type": "command", "command": "/Users/u/Bartender/hooks/agy-notify-hook.sh"}],
                    }
                ],
                "Stop": [{"hooks": [{"type": "command", "command": "/Users/u/Bartender/hooks/agy-notify-hook.sh"}]}],
            }
        }
        top_raw = json.dumps(top_shape, indent=2)
        cfg.write_text(top_raw, encoding="utf-8")
        warnings = audit_agy_hooks_config(cfg)
        self.assertEqual(len(warnings), 1)
        self.assertIn("PreToolUse in hooks.json invokes agy-notify-hook.sh", warnings[0])
        self.assertEqual(cfg.read_text(encoding="utf-8"), top_raw, "audit must never mutate hooks.json")

        # Nested plugin hook map {"bartender-topshelf": {"PreToolUse": [...]}} shape
        nested_shape = {
            "bartender-topshelf": {
                "PreToolUse": [{"command": "~/Library/Application Support/Bartender/NotchBar/AgentStatus/hooks/agy-notify-hook.sh"}],
                "PreInvocation": [{"command": "~/Library/Application Support/Bartender/NotchBar/AgentStatus/hooks/agy-notify-hook.sh"}],
            }
        }
        cfg.write_text(json.dumps(nested_shape), encoding="utf-8")
        nested_warnings = audit_agy_hooks_config(cfg)
        self.assertEqual(nested_warnings, warnings)

        # Duplicate occurrences deduplicated to a single warning
        both_shapes = {"hooks": top_shape["hooks"], "plugins": nested_shape}
        cfg.write_text(json.dumps(both_shapes), encoding="utf-8")
        self.assertEqual(audit_agy_hooks_config(cfg), warnings)

    def test_audit_handles_invalid_json_and_ignores_symlink(self):
        cfg = self.tmp / "broken-hooks.json"
        cfg.write_text("{not valid json", encoding="utf-8")
        self.assertEqual(audit_agy_hooks_config(cfg), ("Antigravity hooks.json is unreadable or invalid JSON",))

        link = self.tmp / "symlink-hooks.json"
        link.symlink_to(cfg)
        self.assertEqual(audit_agy_hooks_config(link), ())

    def test_reexports_present_on_hooks_and_hooks_integrity(self):
        for attr in (
            "AGY_HOOK_NAME",
            "install_agy_hook",
            "uninstall_agy_hook",
            "verify_agy_hook_intact",
            "audit_agy_hooks_config",
        ):
            self.assertTrue(hasattr(hooks, attr), attr)
        self.assertIs(hooks_integrity.verify_agy_hook_intact, agy_hook.verify_agy_hook_intact)


if __name__ == "__main__":
    unittest.main()
