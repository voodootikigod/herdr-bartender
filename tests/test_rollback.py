"""scripts/rollback.sh (Plan §9.1, adapted) run end-to-end inside the sandbox.

`herdr` and `pkill` are stubs on PATH (pkill only logs, it never signals real
processes); Bartender is the mock bridge plus a fake `Bartender 6` process.
"""

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from herdr_bartender.cache import BoundedSessionCache
from herdr_bartender.hooks import install_hooks
from herdr_bartender.paths import get_state_dir
from tests.support import REPO_ROOT, SandboxTestCase
from tests.support.guard_harness import make_shim, path_with, spawn_group
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
        # rollback.sh removes only a real directory named herdr-bartender (R53): use the plugin's own name here.
        os.environ["HERDR_PLUGIN_STATE_DIR"] = str(self.tmp / "plugin-state" / "herdr-bartender")
        self.state_dir = get_state_dir()
        self._assert_sandboxed(self.state_dir)
        self.cache_mgr = BoundedSessionCache(self.state_dir)
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

    def test_fallback_strip_restores_a_monolith_layout(self):
        """R37: the inline strip also removes the blank line the old monolith inserted before BEGIN."""
        from tests.test_hooks_legacy_upgrade import monolith_install
        for name, original in ((CLAUDE_HOOK, VENDOR_CLAUDE), (CODEX_HOOK, VENDOR_CODEX)):
            seed_hook(self.vendor_hooks_dir, name, monolith_install(original), 0o755)
        self.rollback(HERDR_BARTENDER_BIN=str(self.tmp / "missing" / "herdr-bartender"))
        self.assertEqual((self.vendor_hooks_dir / CLAUDE_HOOK).read_bytes(), VENDOR_CLAUDE)
        self.assertEqual((self.vendor_hooks_dir / CODEX_HOOK).read_bytes(), VENDOR_CODEX)

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
        """Plan §2.2/§9.1: a state dir resolving to $HOME (or /) is never created into or removed.

        Round-2 low finding: only three exact spellings were refused, so ``$HOME//``, ``$HOME/.``, ``$HOME/..``, an
        ancestor of $HOME, a symlink to $HOME or a relative path reached ``rm -rf``. Every case stays in the sandbox
        (``self.tmp`` is $HOME's parent)."""
        marker = self.home / "keep.txt"
        marker.write_text("precious")
        sibling = self.tmp / "keep-sibling.txt"
        sibling.write_text("precious")
        link = self.tmp / "link-to-home"
        link.symlink_to(self.home, target_is_directory=True)
        (self.home / "sub").mkdir()
        home = str(self.home)
        for spelling in (home, home + "/", home + "//", home + "/.", home + "/..", home + "/sub/..", str(self.tmp),
                         str(self.tmp) + "//" + self.home.name + "///", str(link), "relative-state"):
            with self.subTest(state_dir=spelling):
                res = self.rollback(HERDR_PLUGIN_STATE_DIR=spelling)
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("refusing", res.stdout)
                self.assertEqual(marker.read_text(), "precious")
                self.assertEqual(sibling.read_text(), "precious")
                self.assertFalse((self.home / "DISABLED").exists() or (self.tmp / "DISABLED").exists())
                self.assertFalse((self.tmp / "relative-state").exists())

    def test_broad_or_shared_state_dir_override_is_never_removed(self):
        """R53 (gate finding, review round 3): `rm -rf` reached any absolute HERDR_PLUGIN_STATE_DIR that is not $HOME
        or its ancestor (/tmp, /var, ~/Library, ...). Only a real directory named herdr-bartender is removed now; any
        other override - or a symlink, even one named herdr-bartender - is kept with exit 1 and DISABLED in place."""
        self.add_fake_process("Bartender 6", live=True)
        shared = self.tmp / "shared"
        shared.mkdir()
        (shared / "keep.txt").write_text("precious")
        link_parent = self.tmp / "links"
        link_parent.mkdir()
        named_link = link_parent / "herdr-bartender"
        named_link.symlink_to(shared, target_is_directory=True)
        for spelling in (str(shared), str(shared) + "/", str(named_link), str(named_link) + "/",
                         str(self.tmp / "herdr-bartender-old")):
            with self.subTest(state_dir=spelling):
                res = self.rollback(HERDR_PLUGIN_STATE_DIR=spelling)
                self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
                self.assertIn("refusing to remove", res.stdout)
                self.assertEqual((shared / "keep.txt").read_text(), "precious")
                self.assertTrue(os.path.lexists(named_link))
                self.assertTrue((Path(spelling) / "DISABLED").exists(), "the tombstone stays")

    def test_trailing_slash_spelling_of_the_plugin_dir_is_removed(self):
        """Control for R53: the plugin's own directory is still removed however it is spelled."""
        self.add_fake_process("Bartender 6", live=True)
        res = self.rollback(HERDR_PLUGIN_STATE_DIR=str(self.state_dir) + "//")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertFalse(self.state_dir.exists())

    EVENT_CMDLINE = "/usr/bin/python3 /Users/u/hb/bin/herdr-bartender pane.agent_status_changed"

    def _cleanup_stub(self):
        """A launcher stub that records whether the fake event handler was still listed when --cleanup ran."""
        return make_shim(self.tmp / "stub-bin3", "hb",
                         'if [ "${1:-}" = --cleanup ]; then\n'
                         '  if grep -q "pane.agent_status_changed" "$HB_TEST_SANDBOX/procs.list"; then\n'
                         '    echo during >> "$HB_TEST_SANDBOX/cleanup.log"; else echo after >> "$HB_TEST_SANDBOX/cleanup.log"; fi\n'
                         'fi\nexit 0')

    def test_waits_for_in_flight_event_handlers_before_cleanup(self):
        """Gate finding (review round 11): an event that passed the DISABLED check before the tombstone could still
        send (and recreate state) while --cleanup ran. Rollback now waits for such handlers (each bounded by the 1.5s
        process deadline) before --cleanup, so cleanup's Ended is the last word."""
        import threading
        stub = self._cleanup_stub()
        self.add_fake_process("herdr-bartender", pid=777001, cmdline=self.EVENT_CMDLINE)
        timer = threading.Timer(0.6, self.clear_fake_processes)
        timer.start()
        self.addCleanup(timer.cancel)
        res = self.rollback(HERDR_BARTENDER_BIN=str(stub))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.log_lines("cleanup.log"), ["after"])

    def test_event_handler_that_never_ends_keeps_the_state(self):
        stub = self._cleanup_stub()
        self.add_fake_process("herdr-bartender", pid=777002, cmdline=self.EVENT_CMDLINE)
        res = self.rollback(HERDR_BARTENDER_BIN=str(stub))
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("event handlers still running", res.stdout)
        self.assertTrue((self.state_dir / "DISABLED").exists())
        self.assertEqual(self.log_lines("cleanup.log"), [], "R68: --cleanup never runs under a live event handler")
        self.assertFalse(any("reconcile-background" in line for line in self.log_lines("pkill.log")),
                         "R68: nothing after the drain runs")

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


def script_pattern(name: str) -> str:
    """A ``NAME='...'`` pattern exactly as scripts/rollback.sh defines it (evaluated by bash)."""
    res = subprocess.run(["bash", "-c", 'eval "$(grep "^$2=" "$1")"; eval "printf %s \\"\\$$2\\""',
                          "x", str(ROLLBACK), name], capture_output=True, text=True, check=True)
    return res.stdout


def reconciler_pattern() -> str:
    """RECONCILER_PATTERN exactly as scripts/rollback.sh defines it (evaluated by bash)."""
    return script_pattern("RECONCILER_PATTERN")


class ReconcilerKillPatternTests(SandboxTestCase):
    """Round-4 low finding: ``pkill -9 -f "herdr-bartender --reconcile-background"`` SIGKILLed any of the user's
    processes whose command line merely contained the string (a grep, an editor, a shell). The pattern now
    matches only a Python interpreter running a .../herdr-bartender launcher that ends with the flag."""

    start_bridge = False
    RECONCILERS = (
        "/usr/bin/python3 /Users/u/Projects/herdr-bartender/bin/herdr-bartender --reconcile-background",
        "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app"
        "/Contents/MacOS/Python /Users/u/Projects/herdr-bartender/bin/herdr-bartender --reconcile-background --foreground",
        "python3 /Users/u/My Projects/hb/bin/herdr-bartender --reconcile-background --foreground",
        "/opt/homebrew/opt/python@3.12/bin/python3.12 /x/bin/herdr-bartender --reconcile-background",
    )
    BYSTANDERS = (
        "vim herdr-bartender --reconcile-background",
        "grep -r herdr-bartender --reconcile-background /tmp",
        "bash -c sleep 30 herdr-bartender --reconcile-background",
        "less /x/bin/herdr-bartender --reconcile-background",
        "/usr/bin/python3 /x/bin/herdr-bartender --reconcile-background-notes.txt",
        "/usr/bin/python3 /x/bin/herdr-bartender --reconcile-background notes.txt",
        "/usr/bin/python3 /x/bin/herdr-bartender --cleanup",
    )

    def _matches(self, pattern: str, line: str) -> bool:
        res = subprocess.run(["grep", "-Eq", "--", pattern], input=line + "\n", text=True)
        return res.returncode == 0

    def test_pattern_matches_reconcilers_and_spares_bystanders(self):
        pattern = reconciler_pattern()
        for line in self.RECONCILERS:
            with self.subTest(reconciler=line):
                self.assertTrue(self._matches(pattern, line))
        for line in self.BYSTANDERS:
            with self.subTest(bystander=line):
                self.assertFalse(self._matches(pattern, line))

    def test_pattern_matches_the_argv_this_package_spawns(self):
        from herdr_bartender.handoff import loop_argv, reconciler_argv
        for argv in (loop_argv(), reconciler_argv()):
            with self.subTest(argv=argv):
                self.assertTrue(self._matches(reconciler_pattern(), " ".join(argv)))

    @unittest.skipUnless(os.path.exists("/usr/bin/pgrep") or os.path.exists("/bin/pgrep"), "needs a real pgrep")
    def test_real_pgrep_selects_only_the_reconciler_process(self):
        """The real (non-shim) pgrep -f with the script's pattern: the fake reconciler is selected, a shell whose
        arguments contain the old substring is not. Nothing is signalled."""
        real_pgrep = "/usr/bin/pgrep" if os.path.exists("/usr/bin/pgrep") else "/bin/pgrep"
        launcher = self.tmp / "bin" / "herdr-bartender"
        launcher.parent.mkdir()
        launcher.write_text("import time\ntime.sleep(30)\n")
        procs = {   # process groups: killing only the bystander bash would orphan its `sleep 30`
            "reconciler": spawn_group(self, [sys.executable, str(launcher), "--reconcile-background", "--foreground"]),
            "bystander": spawn_group(self, ["bash", "-c", "sleep 30; :", "herdr-bartender --reconcile-background"]),
        }
        time.sleep(0.2)
        res = subprocess.run([real_pgrep, "-u", str(os.getuid()), "-f", reconciler_pattern()],
                             capture_output=True, text=True)
        selected = {int(pid) for pid in res.stdout.split()}
        self.assertIn(procs["reconciler"].pid, selected)
        self.assertNotIn(procs["bystander"].pid, selected)

    def test_event_pattern_matches_event_handlers_only(self):
        """The drain's EVENT_PATTERN selects the launcher run with a Herdr event name (as herdr-plugin.toml runs it),
        never a reconciler or a process that merely mentions the string."""
        pattern = script_pattern("EVENT_PATTERN")
        for line in ("python3 ./bin/herdr-bartender pane.agent_status_changed",
                     "/usr/bin/python3 /Users/u/hb/bin/herdr-bartender pane.closed",
                     "/Library/Frameworks/Python.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python "
                     "/x/bin/herdr-bartender workspace.closed"):
            with self.subTest(handler=line):
                self.assertTrue(self._matches(pattern, line))
        for line in ("/usr/bin/python3 /x/bin/herdr-bartender --reconcile-background",
                     "grep -r herdr-bartender pane.closed /tmp", "vim /x/bin/herdr-bartender pane.closed",
                     "/usr/bin/python3 /x/bin/herdr-bartender pane.closed extra"):
            with self.subTest(bystander=line):
                self.assertFalse(self._matches(pattern, line))

    def test_rollback_passes_the_pattern_to_pkill(self):
        stub_bin = self.tmp / "kill-bin"
        make_shim(stub_bin, "pkill", 'printf "%s\\n" "$*" >> "$HB_TEST_SANDBOX/pkill.log"\nexit 1')
        env = {**os.environ, "PATH": path_with(stub_bin), "HERDR_BARTENDER_BIN": str(self.tmp / "missing-bin")}
        subprocess.run(["bash", str(ROLLBACK)], capture_output=True, text=True, env=env, cwd=str(self.tmp),
                       stdin=subprocess.DEVNULL, timeout=60)
        lines = (self.sandbox / "pkill.log").read_text().splitlines()
        self.assertTrue(lines)
        for line in lines:
            self.assertEqual(line, f"-9 -u {os.getuid()} -f {reconciler_pattern()}")


if __name__ == "__main__":
    unittest.main()
