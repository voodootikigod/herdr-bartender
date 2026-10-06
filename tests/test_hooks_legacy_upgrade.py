"""R37: upgrading over vendor hooks the pre-package monolith patched.

Finding (operability): the monolith inserted ``"\\n" + block + "\\n"`` after its anchor line and recorded the
SHA of the ORIGINAL hook. The package's strip kept that blank line, so the first reconciler pass after an
upgrade raised a false HOOK_NEEDS_REVIEW (osascript alert), never replaced the stale guard, ``--health``
called it intact, and uninstall / rollback left the hook one blank line off its original.
"""

import unittest

from herdr_bartender import hooks
from herdr_bartender.background import run_reconcile_background
from herdr_bartender.hooks import (
    STATUS_INTACT,
    STATUS_NEEDS_PATCH,
    STATUS_NEEDS_REVIEW,
    check_hook_integrity,
    install_hooks,
    repair_hooks_if_allowlisted,
    uninstall_hooks,
    verify_vendor_hooks_intact,
)
from herdr_bartender.hooks_text import insert_guard, is_legacy_layout, strip_guard
from tests.support import SandboxTestCase
from tests.support.hook_fixtures import (
    BEGIN,
    CLAUDE_HOOK,
    CODEX_HOOK,
    END,
    VENDOR_CLAUDE,
    VENDOR_CODEX,
    call_quietly,
    read_allowlist,
    seed_hook,
    sha256,
    write_allowlist,
)

T = hooks.HOOK_GUARD_TEMPLATE.encode("utf-8")
ORIGINALS = {CLAUDE_HOOK: VENDOR_CLAUDE, CODEX_HOOK: VENDOR_CODEX}
# A stand-in for the monolith's guard body (its R15-violating loose liveness probe).
LEGACY_BLOCK = BEGIN + b'\nif pgrep -f "Herdr.app" >/dev/null 2>&1; then :; fi\n' + END


def monolith_install(content: bytes, block: bytes = LEGACY_BLOCK) -> bytes:
    """main:bin/herdr-bartender install_hooks: after the exact ``set -u`` line, else after the shebang, insert
    ``"\\n" + block + "\\n"``."""
    lines = content.splitlines(keepends=True)
    idx = 0
    for i, line in enumerate(lines):
        if line.strip() == b"set -u":
            idx = i + 1
            break
        if line.startswith(b"#!") and idx == 0:
            idx = i + 1
    return b"".join(lines[:idx]) + b"\n" + block + b"\n" + b"".join(lines[idx:])


def monolith_reinstall_sha(content: bytes) -> str:
    """A monolith re-install hashed ``split(BEGIN)[0] + split(END)[1]`` of its own layout."""
    legacy = monolith_install(content)
    return sha256(legacy.split(BEGIN)[0] + legacy.split(END)[1])


class LegacyLayoutCase(SandboxTestCase):
    start_bridge = True

    def setUp(self):
        super().setUp()
        self.hooks_dir = self.vendor_hooks_dir
        self.hnr = self.state_dir / "HOOK_NEEDS_REVIEW"

    def seed_monolith(self, allowlist=None, pristine=True):
        """Both hooks as the monolith left them: legacy layout, its allowlist, its .pristine copies."""
        paths = {name: seed_hook(self.hooks_dir, name, monolith_install(original))
                 for name, original in ORIGINALS.items()}
        if pristine:
            for name, original in ORIGINALS.items():
                seed_hook(self.hooks_dir, name + ".pristine", original)
        write_allowlist(self.state_dir, allowlist or {name: sha256(o) for name, o in ORIGINALS.items()})
        return paths


class LegacyIntegrityTests(LegacyLayoutCase):
    def test_first_install_allowlist_is_repaired_without_review(self):
        """The reproduced upgrade: a reconciler pass re-lays the hooks out canonically with the current guard;
        no HOOK_NEEDS_REVIEW, no alert, allowlist untouched, and the next check is intact."""
        paths = self.seed_monolith()
        self.assertTrue(all(is_legacy_layout(p.read_bytes()) for p in paths.values()))
        result = check_hook_integrity()
        self.assertEqual(result.status, STATUS_NEEDS_PATCH)
        self.assertEqual({h.name: (h.status, h.allowlisted) for h in result.hooks},
                         {CLAUDE_HOOK: ("stale_guard", True), CODEX_HOOK: ("stale_guard", True)})
        allowlist = read_allowlist(self.state_dir)
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        for name, original in ORIGINALS.items():
            self.assertEqual(paths[name].read_bytes(), insert_guard(original, T), name)
        self.assertFalse(self.hnr.exists(), "an unchanged vendor hook is no review case")
        self.assertEqual(self.osascript_calls(), [])
        self.assertEqual(read_allowlist(self.state_dir), allowlist, "repair never approves")
        self.assertEqual(check_hook_integrity().status, STATUS_INTACT)

    def test_monolith_reinstall_allowlist_is_repaired_and_stays_intact(self):
        """A monolith re-install approved the hook with two blank lines at the anchor: the repair puts the current
        guard over exactly those approved bytes, so later checks stay intact (no review flapping)."""
        paths = self.seed_monolith({name: monolith_reinstall_sha(o) for name, o in ORIGINALS.items()})
        self.assertEqual(check_hook_integrity().status, STATUS_NEEDS_PATCH)
        result = repair_hooks_if_allowlisted()
        self.assertEqual(sorted(result.repaired), sorted(ORIGINALS))
        for name, original in ORIGINALS.items():
            self.assertEqual(sha256(strip_guard(paths[name].read_bytes())), monolith_reinstall_sha(original))
            self.assertFalse(is_legacy_layout(paths[name].read_bytes()))
        self.assertEqual(check_hook_integrity().status, STATUS_INTACT)
        self.assertEqual(repair_hooks_if_allowlisted().repaired, ())

    def test_legacy_layout_with_the_current_block_is_still_re_laid_out(self):
        """An explicit install with the pre-R37 strip swapped the block in place: current guard, legacy blank line,
        allowlisted with that blank line. It is re-laid out (not reported intact) and then stays intact."""
        approved = {}
        for name, original in ORIGINALS.items():
            seed_hook(self.hooks_dir, name, monolith_install(original, block=T))
            legacy = monolith_install(original, block=T)
            approved[name] = sha256(legacy.split(BEGIN)[0] + legacy.split(END)[1][1:])   # one blank line kept
        write_allowlist(self.state_dir, approved)
        result = check_hook_integrity()
        self.assertEqual((result.status, {h.status for h in result.hooks}), (STATUS_NEEDS_PATCH, {"stale_guard"}))
        repair_hooks_if_allowlisted()
        for name in ORIGINALS:
            self.assertFalse(is_legacy_layout((self.hooks_dir / name).read_bytes()))
        self.assertEqual(check_hook_integrity().status, STATUS_INTACT)

    def test_modified_vendor_content_still_needs_review(self):
        """Fail closed is kept: the legacy variants differ only by blank lines at the insertion point."""
        paths = self.seed_monolith()
        paths[CODEX_HOOK].write_bytes(paths[CODEX_HOOK].read_bytes() + b"echo vendor-update\n")
        self.assertEqual(check_hook_integrity().status, STATUS_NEEDS_REVIEW)

    def test_health_reports_stale_and_legacy_guards(self):
        """``--health`` compares against the current template: a stale or legacy-layout guard is not intact."""
        paths = self.seed_monolith()
        self.assertEqual(verify_vendor_hooks_intact(), (False, [CLAUDE_HOOK, CODEX_HOOK]))
        paths[CLAUDE_HOOK].write_bytes(insert_guard(VENDOR_CLAUDE, T.replace(b"umask 077", b"umask 022")))
        paths[CODEX_HOOK].write_bytes(insert_guard(VENDOR_CODEX, T))
        self.assertEqual(verify_vendor_hooks_intact(), (False, [CLAUDE_HOOK]))
        paths[CLAUDE_HOOK].write_bytes(insert_guard(VENDOR_CLAUDE, T))
        self.assertEqual(verify_vendor_hooks_intact(), (True, []))


class LegacyInstallerTests(LegacyLayoutCase):
    def test_explicit_install_canonicalises_and_approves_the_original(self):
        paths = self.seed_monolith(allowlist={CLAUDE_HOOK: "0" * 64, CODEX_HOOK: "0" * 64})
        ok, out = call_quietly(install_hooks)
        self.assertTrue(ok, out)
        for name, original in ORIGINALS.items():
            self.assertEqual(paths[name].read_bytes(), insert_guard(original, T))
        self.assertEqual(read_allowlist(self.state_dir), {name: sha256(o) for name, o in ORIGINALS.items()})

    def test_uninstall_restores_the_original_and_drops_the_matching_pristine(self):
        paths = self.seed_monolith()
        ok, out = call_quietly(uninstall_hooks)
        self.assertTrue(ok, out)
        for name, original in ORIGINALS.items():
            self.assertEqual(paths[name].read_bytes(), original, name)
            self.assertFalse((self.hooks_dir / (name + ".pristine")).exists(), "the .pristine matches: removed")

    def test_insert_strip_round_trip_is_unchanged(self):
        """The canonical layout never puts a blank line before BEGIN, so R37 cannot alter a package install."""
        for original in (*ORIGINALS.values(), b"#!/bin/sh\n\nset -u\n\necho x\n", b"#!/bin/bash\n\n\necho y\n"):
            with self.subTest(original=original[:24]):
                patched = insert_guard(original, T)
                self.assertFalse(is_legacy_layout(patched))
                self.assertEqual(strip_guard(patched), original)


if __name__ == "__main__":
    unittest.main()
