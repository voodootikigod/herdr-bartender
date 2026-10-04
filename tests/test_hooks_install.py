"""Vendor hook installer / uninstaller and the shared guard text helpers.

The vendor hooks dir is sandboxed via HERDR_BARTENDER_VENDOR_HOOKS_DIR; nothing
here can reach the real Bartender hooks.
"""

import os
import subprocess
import unittest
from unittest import mock

from herdr_bartender import hooks, hooks_fs
from herdr_bartender.hooks import HOOK_GUARD_TEMPLATE, install_hooks, uninstall_hooks
from herdr_bartender.hooks_text import (
    AnchorError,
    MarkerError,
    guard_state,
    insert_guard,
    replace_guard,
    strip_guard,
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

T = HOOK_GUARD_TEMPLATE.encode("utf-8")
REVIEW_WARNING = (
    "[WARNING] Vendor hook modified upstream (SHA mismatch). "
    "Run 'herdr-bartender --install-hooks' to re-verify and approve changes."
)


class GuardTextTests(unittest.TestCase):
    """Pure byte-level insert/strip helpers shared by install, uninstall, reconciler and rollback."""

    ROUND_TRIP = {
        "set-u": b"#!/bin/bash\nset -u\necho hi\n",
        "set-eu": b"#!/bin/bash\nset -eu\necho hi\n",
        "set-euo-pipefail": b"#!/usr/bin/env bash\nset -euo pipefail\necho hi\n",
        "set-o-nounset": b"#!/bin/bash\nset -o nounset\necho hi\n",
        "shebang-only-anchor": b"#!/bin/sh\necho hi\n",
        "no-final-newline": b"#!/bin/bash\nset -u\necho hi",
        "crlf-body": b"#!/bin/bash\nset -u\r\necho hi\r\n",
        "non-utf8": b"#!/bin/bash\nset -u\necho '\xff\xfe\x80'\n",
        "blank-lines": b"#!/bin/bash\n\n\nset -u\n\n\necho hi\n\n",
    }

    def test_insert_strip_round_trip_is_byte_exact(self):
        """Plan §7.3 (gap strip-not-inverse-of-insert): strip_guard(insert_guard(x)) == x for every anchor style."""
        for label, original in self.ROUND_TRIP.items():
            with self.subTest(case=label):
                patched = insert_guard(original, T)
                self.assertEqual(patched.count(T), 1)
                self.assertEqual(strip_guard(patched), original)
                self.assertEqual(guard_state(patched), "present")
                self.assertEqual(guard_state(original), "absent")

    def test_anchor_placement(self):
        """Plan §7.2/§7.3 (gap anchor-detection-narrow): guard goes right after the nounset line, else the shebang."""
        cases = {
            b"#!/bin/bash\nset -u\necho hi\n": b"#!/bin/bash\nset -u\n",
            b"#!/bin/bash\nset -euo pipefail # strict\necho hi\n": b"#!/bin/bash\nset -euo pipefail # strict\n",
            b"#!/bin/bash\n# header\nset -e\nset -u\necho hi\n": b"#!/bin/bash\n# header\nset -e\nset -u\n",
            b"#!/bin/bash\necho start\nset -u\n": b"#!/bin/bash\n",
            b"#!/bin/bash\nf() {\n  set -u\n}\n": b"#!/bin/bash\n",
            b"#!/bin/bash\nset +u\necho hi\n": b"#!/bin/bash\n",
            b"#!/bin/bash\nset -e\necho hi\n": b"#!/bin/bash\n",
        }
        for original, prefix in cases.items():
            with self.subTest(original=original):
                patched = insert_guard(original, T)
                self.assertTrue(patched.startswith(prefix + T + b"\n"), patched[:80])

    def test_anchor_rejections(self):
        """Plan §7.3 (gap anchor-detection-narrow): shebang only on line 1, bash/sh only; otherwise abort."""
        for bad in (
            b"# comment first\n#!/bin/bash\necho hi\n",
            b"#!/usr/bin/env python3\nprint('hi')\n",
            b"#!/bin/zsh\nset -u\n",
            b"echo no anchor\n",
            b"#!/bin/bash",
            b"",
        ):
            with self.subTest(content=bad):
                with self.assertRaises(AnchorError):
                    insert_guard(bad, T)

    def test_marker_count_validation(self):
        """Plan §9.1 (gap marker-count-validation): exactly one BEGIN and one END, BEGIN first, each on its own line."""
        guarded = insert_guard(VENDOR_CLAUDE, T)
        begin = b"# BEGIN HERDR-BARTENDER DEDUP GUARD"
        end = b"# END HERDR-BARTENDER DEDUP GUARD"
        malformed = {
            "two-guards": guarded + T + b"\n",
            "begin-only": VENDOR_CLAUDE + begin + b"\n",
            "end-only": VENDOR_CLAUDE + end + b"\n",
            "end-before-begin": b"#!/bin/bash\n" + end + b"\n" + begin + b"\n",
            "begin-mid-line": b"#!/bin/bash\necho x " + begin + b"\n" + end + b"\n",
            "trailing-text-after-end": b"#!/bin/bash\n" + begin + b"\n" + end + b" junk\n",
        }
        for label, content in malformed.items():
            with self.subTest(case=label):
                for fn in (guard_state, strip_guard):
                    with self.assertRaises(MarkerError):
                        fn(content)
                with self.assertRaises(MarkerError):
                    insert_guard(content, T)

    def test_replace_guard_updates_stale_block_in_place(self):
        """Plan §7.3: re-install swaps an outdated guard block without moving it or touching vendor bytes."""
        stale_t = T.replace(b"umask 077", b"umask 022")
        stale = insert_guard(VENDOR_CODEX, stale_t)
        fresh = replace_guard(stale, T)
        self.assertEqual(fresh, insert_guard(VENDOR_CODEX, T))
        self.assertEqual(strip_guard(fresh), VENDOR_CODEX)


class HookInstallTests(SandboxTestCase):
    start_bridge = False

    def setUp(self):
        super().setUp()
        self.hooks_dir = self.vendor_hooks_dir
        self.sha_file = self.state_dir / "vendor-hook-sha.json"
        self.flags = [self.state_dir / n for n in ("HOOK_NEEDS_REVIEW", ".hook_review_alerted")]

    def install(self):
        return call_quietly(install_hooks)

    def uninstall(self):
        return call_quietly(uninstall_hooks)

    def test_p22_install_sets_exec_bit_and_records_state(self):
        """Plan §10.1 #22 (gap t22-install-mode / hollow-install-tests): install_hooks keeps orig_mode | 0o100."""
        paths = seed_vendor_hooks(self.hooks_dir, claude_mode=0o644, codex_mode=0o750)
        originals = {CLAUDE_HOOK: (VENDOR_CLAUDE, 0o644), CODEX_HOOK: (VENDOR_CODEX, 0o750)}
        ok, out = self.install()
        self.assertTrue(ok, out)
        for name, (content, orig_mode) in originals.items():
            with self.subTest(hook=name):
                hook = paths[name]
                self.assertEqual(mode_of(hook), orig_mode | 0o100)
                self.assertEqual(guard_count(hook), (1, 1))
                self.assertEqual(strip_guard(hook.read_bytes()), content)
                pristine = hook.with_name(name + ".pristine")
                self.assertEqual(pristine.read_bytes(), content)
                self.assertEqual(mode_of(pristine), orig_mode | 0o100)
        self.assertEqual(read_allowlist(self.state_dir), {CLAUDE_HOOK: sha256(VENDOR_CLAUDE), CODEX_HOOK: sha256(VENDOR_CODEX)})
        self.assertEqual(mode_of(self.sha_file), 0o600)

        snapshot = {n: p.read_bytes() for n, p in paths.items()}
        ok, out = self.install()
        self.assertTrue(ok, out)
        self.assertEqual({n: p.read_bytes() for n, p in paths.items()}, snapshot, "second install must be idempotent")

        ok, out = self.uninstall()
        self.assertTrue(ok, out)
        for name, (content, orig_mode) in originals.items():
            with self.subTest(uninstalled=name):
                self.assertEqual(paths[name].read_bytes(), content, "uninstall must restore the exact bytes")
                self.assertEqual(mode_of(paths[name]), orig_mode | 0o100, "R17: uninstall preserves the mode exactly")
        self.assertEqual(tmp_leftovers(self.hooks_dir), [])

    def test_p22_executes_patched_hook(self):
        """Plan §10.1 #22: a patched hook is still a working executable vendor hook (fall-through path)."""
        paths = seed_vendor_hooks(self.hooks_dir, claude_mode=0o644)
        self.install()
        res = subprocess.run([str(paths[CLAUDE_HOOK])], input="payload", capture_output=True, text=True,
                             env={**os.environ, "HERDR_PANE_ID": "w1:pInstalled"}, timeout=20)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "claude:payload\n")

    def test_install_aborts_without_anchor(self):
        """Plan §7.3 (gap hollow-install-tests): no anchor -> file unchanged, no SHA approval, install fails."""
        bad = b"echo no shebang here\n"
        hook = seed_hook(self.hooks_dir, CLAUDE_HOOK, bad, 0o755)
        seed_hook(self.hooks_dir, CODEX_HOOK, VENDOR_CODEX, 0o755)
        ok, out = self.install()
        self.assertFalse(ok)
        self.assertIn("anchor", out)
        self.assertEqual(hook.read_bytes(), bad)
        self.assertEqual(read_allowlist(self.state_dir), {CODEX_HOOK: sha256(VENDOR_CODEX)},
                         "gap install-tmp-and-sha-file-hygiene: only successfully patched hooks are approved")

    def test_install_aborts_on_bash_syntax_error(self):
        """Plan §7.3 (gap hollow-install-tests): a bash -n failure leaves the file unchanged and no tmp file."""
        broken = b"#!/bin/bash\nset -u\nif then fi\n"
        hook = seed_hook(self.hooks_dir, CLAUDE_HOOK, broken, 0o755)
        ok, out = self.install()
        self.assertFalse(ok)
        self.assertIn("Syntax validation failed", out)
        self.assertEqual(hook.read_bytes(), broken)
        self.assertEqual(tmp_leftovers(self.hooks_dir), [])
        self.assertFalse(self.sha_file.exists())

    def test_install_aborts_on_concurrent_modification(self):
        """Plan §7.3 (gap hollow-install-tests): a write between prepare and replace aborts the patch."""
        hook = seed_hook(self.hooks_dir, CLAUDE_HOOK, VENDOR_CLAUDE, 0o755)
        concurrent = VENDOR_CLAUDE + b"# vendor update\n"
        real_check = hooks_fs.bash_syntax_ok

        def racing_check(path):
            hook.write_bytes(concurrent)
            return real_check(path)

        with mock.patch.object(hooks_fs, "bash_syntax_ok", side_effect=racing_check):
            ok, out = self.install()
        self.assertFalse(ok)
        self.assertIn("modified on disk", out)
        self.assertEqual(hook.read_bytes(), concurrent)
        self.assertEqual(tmp_leftovers(self.hooks_dir), [])

    def test_install_cleans_tmp_when_validation_raises(self):
        """Plan §7.3 (gap install-tmp-and-sha-file-hygiene): tmp.<pid> is unlinked when an exception escapes."""
        hook = seed_hook(self.hooks_dir, CLAUDE_HOOK, VENDOR_CLAUDE, 0o755)
        with mock.patch.object(hooks_fs.subprocess, "run", side_effect=OSError("bash missing")):
            ok, out = self.install()
        self.assertFalse(ok)
        self.assertEqual(hook.read_bytes(), VENDOR_CLAUDE)
        self.assertEqual(tmp_leftovers(self.hooks_dir), [])

    def test_install_refuses_malformed_markers(self):
        """Plan §9.1 (gap marker-count-validation): duplicate or unterminated guards are never touched."""
        cases = {
            "duplicate": insert_guard(VENDOR_CLAUDE, T) + T + b"\n",
            "begin-only": VENDOR_CLAUDE + b"# BEGIN HERDR-BARTENDER DEDUP GUARD\n",
        }
        for label, content in cases.items():
            with self.subTest(case=label):
                hook = seed_hook(self.hooks_dir, CLAUDE_HOOK, content, 0o755)
                ok, out = self.install()
                self.assertFalse(ok)
                self.assertIn("mismatched", out)
                self.assertEqual(hook.read_bytes(), content)
                ok, out = self.uninstall()
                self.assertFalse(ok)
                self.assertEqual(hook.read_bytes(), content)

    def test_install_replaces_stale_guard_and_backfills_pristine(self):
        """Plan §7.3 (gap pristine-backup-gaps): .pristine is written from clean content even when a guard exists."""
        stale = insert_guard(VENDOR_CLAUDE, T.replace(b"umask 077", b"umask 022"))
        hook = seed_hook(self.hooks_dir, CLAUDE_HOOK, stale, 0o700)
        ok, out = self.install()
        self.assertTrue(ok, out)
        self.assertEqual(hook.read_bytes(), insert_guard(VENDOR_CLAUDE, T))
        pristine = hook.with_name(CLAUDE_HOOK + ".pristine")
        self.assertEqual(pristine.read_bytes(), VENDOR_CLAUDE)
        self.assertEqual(read_allowlist(self.state_dir)[CLAUDE_HOOK], sha256(VENDOR_CLAUDE))

    def test_install_refreshes_pristine_on_upstream_change(self):
        """Plan §7.3 (gap pristine-backup-gaps): a changed upstream hook refreshes .pristine with target_mode."""
        hook = seed_hook(self.hooks_dir, CLAUDE_HOOK, VENDOR_CLAUDE, 0o640)
        pristine = hook.with_name(CLAUDE_HOOK + ".pristine")
        pristine.write_bytes(b"#!/bin/bash\nold release\n")
        os.chmod(pristine, 0o600)
        ok, out = self.install()
        self.assertTrue(ok, out)
        self.assertEqual(pristine.read_bytes(), VENDOR_CLAUDE)
        self.assertEqual(mode_of(pristine), 0o740)

    def test_install_repairs_corrupt_allowlist_atomically(self):
        """Plan §8 (gap install-tmp-and-sha-file-hygiene): vendor-hook-sha.json is rewritten whole, 0600."""
        seed_vendor_hooks(self.hooks_dir)
        self.sha_file.write_text("{not json")
        ok, out = self.install()
        self.assertTrue(ok, out)
        self.assertEqual(set(read_allowlist(self.state_dir)), {CLAUDE_HOOK, CODEX_HOOK})
        self.assertEqual(mode_of(self.sha_file), 0o600)
        self.assertEqual([p.name for p in self.state_dir.glob("*.tmp*")], [])

    def test_p68_install_hooks_clears_review_flags(self):
        """Plan §10.1 #68 (gap install-flag-unlink-order): install clears the flags even when the hooks dir is missing."""
        no_hooks = self.state_dir / "NO_HOOKS"
        for f in (*self.flags, no_hooks):
            f.touch()
        self.assertFalse(self.hooks_dir.exists())
        ok, _ = self.install()
        self.assertFalse(ok, "nothing was installed")
        for f in (*self.flags, no_hooks):
            self.assertFalse(f.exists(), f"install_hooks must unlink {f.name}")

    def test_p68_successful_install_clears_review_flags(self):
        """Plan §10.1 #68: a successful explicit install approves, clears HOOK_NEEDS_REVIEW and .hook_review_alerted."""
        seed_vendor_hooks(self.hooks_dir)
        for f in self.flags:
            f.touch()
        ok, out = self.install()
        self.assertTrue(ok, out)
        self.assertEqual([f.exists() for f in self.flags], [False, False])

    def test_failed_install_keeps_review_flags_but_clears_no_hooks(self):
        """Plan §7.3 (gap install-flag-unlink-order): a failed install must not hide the review warning."""
        seed_hook(self.hooks_dir, CLAUDE_HOOK, b"echo no anchor\n", 0o755)
        no_hooks = self.state_dir / "NO_HOOKS"
        for f in (*self.flags, no_hooks):
            f.touch()
        ok, _ = self.install()
        self.assertFalse(ok)
        self.assertEqual([f.exists() for f in self.flags], [True, True])
        self.assertFalse(no_hooks.exists(), "explicit install always clears the sticky uninstall intent")

    def test_uninstall_records_sticky_intent_even_without_hooks_dir(self):
        """Plan §7.3 (gap no-hooks-sticky-missing): --uninstall-hooks creates NO_HOOKS before any existence check."""
        self.assertFalse(self.hooks_dir.exists())
        ok, _ = self.uninstall()
        self.assertTrue(ok)
        no_hooks = self.state_dir / "NO_HOOKS"
        self.assertTrue(no_hooks.exists())
        self.assertEqual(mode_of(no_hooks), 0o600)
        self.hooks_dir.mkdir()
        no_hooks.unlink()
        ok, _ = self.uninstall()
        self.assertTrue(ok)
        self.assertTrue(no_hooks.exists(), "NO_HOOKS must be created when the dir exists but has no hooks")

    def test_uninstall_logs_skip_and_keeps_allowlist(self):
        """Plan §7.3 (gap uninstall-skip-not-logged): an unguarded hook is skipped with a message, not silently."""
        paths = seed_vendor_hooks(self.hooks_dir)
        self.install()
        paths[CODEX_HOOK].write_bytes(VENDOR_CODEX)
        ok, out = self.uninstall()
        self.assertTrue(ok, out)
        self.assertIn(f"[=] No dedup guard in {CODEX_HOOK}; skipping", out)
        self.assertEqual(paths[CODEX_HOOK].read_bytes(), VENDOR_CODEX)
        self.assertEqual(paths[CLAUDE_HOOK].read_bytes(), VENDOR_CLAUDE)
        self.assertTrue(self.sha_file.exists(), "allowlist is kept so a later install can detect drift")
        self.assertFalse(paths[CLAUDE_HOOK].with_name(CLAUDE_HOOK + ".pristine").exists(),
                         "a .pristine identical to the restored hook is redundant and removed")

    def test_unreadable_template_does_not_touch_hooks(self):
        """Plan §7.3: a missing hook_guard.sh fails the install without modifying vendor hooks."""
        paths = seed_vendor_hooks(self.hooks_dir)
        with mock.patch.object(hooks, "_GUARD_RESOURCE", self.tmp / "missing.sh"), \
                mock.patch.object(hooks, "_guard_template_cache", None):
            ok, out = self.install()
        self.assertFalse(ok)
        self.assertEqual(paths[CLAUDE_HOOK].read_bytes(), VENDOR_CLAUDE)


class HookCliTests(SandboxTestCase):
    start_bridge = False

    def test_status_prints_exact_review_warning(self):
        """Plan §7.3 (gap hollow-install-tests): --status prints the exact HOOK_NEEDS_REVIEW warning."""
        (self.state_dir / "HOOK_NEEDS_REVIEW").touch()
        res = self.run_cli("--status")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.splitlines()[0], REVIEW_WARNING)

    def test_cli_install_uninstall_round_trip(self):
        """Plan §7.3: --install-hooks then --uninstall-hooks restores bytes and records NO_HOOKS."""
        paths = seed_vendor_hooks(self.vendor_hooks_dir)
        res = self.run_cli("--install-hooks")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(guard_count(paths[CLAUDE_HOOK]), (1, 1))
        res = self.run_cli("--uninstall-hooks")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(paths[CLAUDE_HOOK].read_bytes(), VENDOR_CLAUDE)
        self.assertTrue((self.state_dir / "NO_HOOKS").exists())


if __name__ == "__main__":
    unittest.main()
