"""scripts/rollback.sh (Plan §9.1, adapted) run end-to-end inside the sandbox.

`herdr` and `pkill` are stubs on PATH (pkill only logs, it never signals real
processes); Bartender is the mock bridge plus a fake `Bartender 6` process.
"""

import os
import subprocess
import unittest

from herdr_bartender.hooks import install_hooks
from tests.support import REPO_ROOT, SandboxTestCase
from tests.support.guard_harness import make_shim, path_with
from tests.support.hook_fixtures import (
    CLAUDE_HOOK,
    CODEX_HOOK,
    VENDOR_CLAUDE,
    VENDOR_CODEX,
    call_quietly,
    guard_count,
    mode_of,
    seed_hook,
    seed_vendor_hooks,
)

ROLLBACK = REPO_ROOT / "scripts" / "rollback.sh"
SESSION_ID = "herdr:testhost:w1:pRollback"


class RollbackScriptTests(SandboxTestCase):
    start_bridge = True

    def setUp(self):
        super().setUp()
        self.stub_bin = self.tmp / "rollback-bin"
        make_shim(self.stub_bin, "pkill", 'printf "%s\\n" "$*" >> "$HB_TEST_SANDBOX/pkill.log"\nexit 1')
        self.plugin_src = self.tmp / "plugin-src"
        self.plugin_src.mkdir()
        plugins = self.home / ".config" / "herdr" / "plugins"
        self.links = (plugins / "herdr-bartender", plugins / "local" / "herdr-bartender")
        for link in self.links:
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(self.plugin_src, target_is_directory=True)
        self.orphans = self.home / ".herdr-bartender-orphans.json"

    # -- helpers ---------------------------------------------------------------
    def seed_session(self):
        with self.cache_mgr as data:
            data.setdefault("sessions", {})[SESSION_ID] = {
                "pane_id": "w1:pRollback", "agent": "Claude (Herdr)", "seq": 3,
                "desired_state": "Working", "delivered_state": "Working", "delivered_seq": 3,
                "delivery_status": "delivered",
            }
            self.cache_mgr.save(data)

    def seed_installed_hooks(self):
        paths = seed_vendor_hooks(self.vendor_hooks_dir, claude_mode=0o644, codex_mode=0o750)
        ok, out = call_quietly(install_hooks)
        self.assertTrue(ok, out)
        return paths

    def herdr_lists_plugin(self):
        make_shim(self.stub_bin, "herdr",
                  'printf "%s\\n" "$*" >> "$HB_TEST_SANDBOX/herdr.log"\n'
                  'if [ "${1:-}" = plugin ] && [ "${2:-}" = list ]; then echo "herdr-bartender (local)"; fi\nexit 0')

    def rollback(self, **env_extra):
        env = {**os.environ, "PATH": path_with(self.stub_bin), **env_extra}
        return subprocess.run(["bash", str(ROLLBACK)], capture_output=True, text=True, env=env,
                              cwd=str(self.tmp), stdin=subprocess.DEVNULL, timeout=60)

    def log_lines(self, name):
        log = self.sandbox / name
        return log.read_text().splitlines() if log.exists() else []

    # -- tests -----------------------------------------------------------------
    def test_script_is_shipped_executable_and_syntax_clean(self):
        """Plan §9.1 (gap rollback-script-missing): scripts/rollback.sh exists, is 0755 and `bash -n` clean."""
        self.assertTrue(ROLLBACK.is_file())
        self.assertEqual(mode_of(ROLLBACK), 0o755)
        res = subprocess.run(["bash", "-n", str(ROLLBACK)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("~/Projects/herdr-bartender", ROLLBACK.read_text(), "binary path must be repo-relative")

    def test_success_path_removes_everything(self):
        """Plan §9.1 (gap rollback-script-missing): cleanup exit 0 -> links, guards and state dir are removed."""
        self.add_fake_process("Bartender 6", live=True)
        self.seed_session()
        paths = self.seed_installed_hooks()
        res = self.rollback()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("Rollback completed successfully.", res.stdout)
        self.assertFalse(self.state_dir.exists(), "state dir is removed only on full success")
        for link in self.links:
            self.assertFalse(os.path.lexists(link), f"{link} must be unlinked")
        self.assertTrue(self.plugin_src.is_dir(), "only the symlinks are removed, never their target")
        self.assertEqual(paths[CLAUDE_HOOK].read_bytes(), VENDOR_CLAUDE)
        self.assertEqual(paths[CODEX_HOOK].read_bytes(), VENDOR_CODEX)
        self.assertEqual([e.get("state") for e in self.bridge.events_for(SESSION_ID)], ["Ended"])
        self.assertIn("plugin unlink herdr-bartender", self.log_lines("herdr.log"))
        pkill = self.log_lines("pkill.log")
        self.assertTrue(pkill and all(f"-u {os.getuid()}" in line for line in pkill), pkill)

    def test_bridge_unreachable_keeps_tombstone_and_exports_orphans(self):
        """Plan §9.1 (gap rollback-script-missing): cleanup exit 2 -> tombstone kept, orphan replay hint printed."""
        self.seed_session()
        self.seed_installed_hooks()
        self.bridge.stop()
        res = self.rollback()
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("Notice: Bartender bridge unreachable during cleanup", res.stdout)
        self.assertTrue((self.state_dir / "DISABLED").exists())
        self.assertTrue(self.orphans.exists())
        launcher = REPO_ROOT / "bin" / "herdr-bartender"
        self.assertIn(f"{launcher} --replay-orphans {self.orphans}", res.stdout)

    def test_aborts_when_herdr_still_lists_plugin(self):
        """Plan §9.1 Step 1: a plugin Herdr still lists aborts with exit 1 before cleanup, tombstone active."""
        self.herdr_lists_plugin()
        self.seed_session()
        paths = self.seed_installed_hooks()
        res = self.rollback()
        self.assertEqual(res.returncode, 1)
        self.assertIn("Failed to unlink herdr-bartender plugin", res.stdout)
        self.assertTrue((self.state_dir / "DISABLED").exists())
        self.assertEqual(guard_count(paths[CLAUDE_HOOK]), (1, 1), "hooks are untouched after the early abort")
        self.assertEqual(self.bridge.events_for(SESSION_ID), [])

    def test_fallback_strip_without_binary(self):
        """Plan §9.1 Step 4 (gaps marker-count-validation, strip-not-inverse-of-insert): inline strip is byte-exact."""
        paths = self.seed_installed_hooks()
        broken = VENDOR_CODEX + b"# BEGIN HERDR-BARTENDER DEDUP GUARD\n"
        seed_hook(self.vendor_hooks_dir, CODEX_HOOK, broken, 0o750)
        res = self.rollback(HERDR_BARTENDER_BIN=str(self.tmp / "missing" / "herdr-bartender"))
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertEqual(paths[CLAUDE_HOOK].read_bytes(), VENDOR_CLAUDE)
        self.assertEqual(mode_of(paths[CLAUDE_HOOK]), 0o744, "fallback strip preserves the mode exactly")
        self.assertEqual(paths[CODEX_HOOK].read_bytes(), broken, "mismatched markers are never edited")
        self.assertIn("mismatched dedup markers", res.stdout)
        self.assertEqual(sorted(p.name for p in self.vendor_hooks_dir.glob("*.tmp*")), [])
        self.assertTrue((self.state_dir / "DISABLED").exists())

    def test_binary_override_is_used(self):
        """Plan §9.1 (adapted): HERDR_BARTENDER_BIN overrides the repo-relative launcher."""
        stub = make_shim(self.tmp / "stub-bin", "hb", 'printf "%s\\n" "$*" >> "$HB_TEST_SANDBOX/hb.log"\nexit 0')
        res = self.rollback(HERDR_BARTENDER_BIN=str(stub))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.log_lines("hb.log"), ["--cleanup", "--uninstall-hooks"])

    def test_resolves_launcher_through_symlinked_script(self):
        """Plan §9.1 (adapted): invoked via a symlink, the script still finds <repo>/bin/herdr-bartender."""
        link = self.tmp / "elsewhere" / "rollback"
        link.parent.mkdir()
        link.symlink_to(ROLLBACK)
        seed_vendor_hooks(self.vendor_hooks_dir)
        env = {**os.environ, "PATH": path_with(self.stub_bin)}
        res = subprocess.run(["bash", str(link)], capture_output=True, text=True, env=env, cwd=str(self.tmp),
                             stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNotIn("not executable", res.stdout)
        self.assertFalse(self.state_dir.exists())

    def test_refuses_unsafe_state_dir(self):
        """Plan §2.2/§9.1: a state dir resolving to $HOME (or /) is never created into or removed."""
        marker = self.home / "keep.txt"
        marker.write_text("precious")
        res = self.rollback(HERDR_PLUGIN_STATE_DIR=str(self.home))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("refusing", res.stdout)
        self.assertEqual(marker.read_text(), "precious")
        self.assertFalse((self.home / "DISABLED").exists())

    def test_state_dir_follows_xdg_rule(self):
        """Plan §2.2: without HERDR_PLUGIN_STATE_DIR the script uses $XDG_STATE_HOME/herdr/plugins/herdr-bartender."""
        stub = make_shim(self.tmp / "stub-bin2", "hb", "exit 0")
        env = {k: v for k, v in os.environ.items() if k != "HERDR_PLUGIN_STATE_DIR"}
        xdg_state = self.xdg_state / "herdr" / "plugins" / "herdr-bartender"
        xdg_state.mkdir(parents=True)
        (xdg_state / "active-sessions.json").write_text("{}")
        res = subprocess.run(["bash", str(ROLLBACK)], capture_output=True, text=True, timeout=60,
                             env={**env, "PATH": path_with(self.stub_bin), "HERDR_BARTENDER_BIN": str(stub)},
                             stdin=subprocess.DEVNULL)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertFalse(xdg_state.exists())
        self.assertTrue(self.state_dir.exists(), "HERDR_PLUGIN_STATE_DIR location was not touched")


if __name__ == "__main__":
    unittest.main()
