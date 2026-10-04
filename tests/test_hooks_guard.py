"""Vendor-hook dedup guard (hook_guard.sh) behavior.

Herdr liveness inside the guard is decided by the sandbox pgrep shim; setUp
registers a fake running Herdr so "healthy" paths are reachable on any host.
"""

import json
import os
import subprocess
import time
import unittest

from herdr_bartender.hooks import HOOK_GUARD_TEMPLATE
from herdr_bartender.markers import remove_pane_marker, touch_pane_marker
from herdr_bartender.sanitize import get_hex_pane_id, normalize_pane_id
from tests.support import SandboxTestCase, run_guard, write_guard_script

ENV_SHEBANG = "#!/usr/bin/env bash"


class HookGuardTests(SandboxTestCase):
    start_bridge = False

    def setUp(self):
        super().setUp()
        self.set_herdr_alive()
        (self.state_dir / "panes").mkdir(parents=True, exist_ok=True)

    def _exported_guard(self, name, pane, tail):
        preamble = f'export HERDR_PLUGIN_STATE_DIR="{self.state_dir}"\nexport HERDR_PANE_ID="{pane}"'
        return write_guard_script(self.state_dir / name, tail, preamble=preamble, shebang=ENV_SHEBANG)

    def _fifo_writer(self, fifo_path, shell):
        writer = subprocess.Popen(["bash", "-c", shell])
        self.addCleanup(writer.wait)
        self.addCleanup(writer.kill)
        return writer

    def test_p23_terminal_unlinking_and_healthy_suppression(self):
        """Plan §10.1 #23: argv/stdin terminal handling of .vendor_active and healthy-pane suppression."""
        script = write_guard_script(self.state_dir / "test-guard-exec.sh", 'echo "PASSTHROUGH"')
        v_hex = get_hex_pane_id(normalize_pane_id("pGuardTest", "w1"))
        v_active = self.state_dir / "panes" / f"{v_hex}.vendor_active"
        v_marker = self.state_dir / "panes" / v_hex
        env = {"HERDR_PANE_ID": "pGuardTest", "HERDR_WORKSPACE_ID": "w1"}

        # Step A: fall-through creates .vendor_active
        res = run_guard(script, "Working", env_extra=env)
        self.assertEqual(res.returncode, 0)
        self.assertIn("PASSTHROUGH", res.stdout)
        self.assertTrue(v_active.exists(), ".vendor_active must be created on fall-through")

        # Step B: argv Ended removes .vendor_active and never re-touches it
        res = run_guard(script, "Ended", env_extra=env)
        self.assertEqual(res.returncode, 0)
        self.assertIn("PASSTHROUGH", res.stdout)
        self.assertFalse(v_active.exists(), ".vendor_active must be removed and never re-touched on terminal Ended")

        # Step C: stdin JSON Stop passes through and retains .vendor_active
        v_active.touch()
        self.assertTrue(v_active.exists())
        res = run_guard(script, input=json.dumps({"hook_event_name": "Stop", "session_id": "test_sid_12345678"}), env_extra=env)
        self.assertEqual(res.returncode, 0)
        self.assertIn("PASSTHROUGH", res.stdout)
        self.assertTrue(v_active.exists(), "Turn-terminal Stop must pass through and retain .vendor_active")

        # Step C1: Stop on a UUID-bearing .vendor_active also passes through and retains it
        v_active.write_text('{"vendor_session_id":"test_uuid_9999"}')
        res = run_guard(script, input=json.dumps({"hook_event_name": "Stop", "session_id": "test_uuid_9999"}), env_extra=env)
        self.assertEqual(res.returncode, 0)
        self.assertIn("PASSTHROUGH", res.stdout)
        self.assertTrue(v_active.exists(), "Turn-terminal Stop on UUID record must pass through and retain .vendor_active")

        # Step C2: stdin JSON SessionEnd passes through and clears .vendor_active
        res = run_guard(script, input=json.dumps({"hook_event_name": "SessionEnd", "session_id": "test_sid_12345678"}), env_extra=env)
        self.assertEqual(res.returncode, 0)
        self.assertIn("PASSTHROUGH", res.stdout)
        self.assertFalse(v_active.exists(), "Session-terminal SessionEnd must pass through and remove .vendor_active")

        # Step D: healthy pane (fresh marker, no flags, no .vendor_active, herdr alive)
        v_active.unlink(missing_ok=True)
        v_marker.touch()
        res = run_guard(script, "Stop", env_extra=env)
        self.assertEqual(res.returncode, 0)
        self.assertNotIn("PASSTHROUGH", res.stdout, "Healthy pane must suppress turn-terminal vendor events when .vendor_active is absent")
        self.assertFalse(v_active.exists(), ".vendor_active must not be created on suppressed terminal event")

        res = run_guard(script, "agent-turn-complete", env_extra=env)
        self.assertEqual(res.returncode, 0)
        self.assertNotIn("PASSTHROUGH", res.stdout, "Healthy pane must suppress bare argv agent-turn-complete")

        res = run_guard(script, "session-end", env_extra=env)
        self.assertEqual(res.returncode, 0)
        self.assertIn("PASSTHROUGH", res.stdout, "Session-terminal bare argv session-end must pass through")
        self.assertFalse(v_active.exists())

    def _stat_shim_dir(self, flavor):
        shim_dir = self.tmp / f"stat-{flavor}"
        shim_dir.mkdir()
        mtime = 'exec python3 -c "import os,sys; print(int(os.stat(sys.argv[1]).st_mtime))" "$3"'
        bodies = {
            "gnu": f'if [ "$1" = "-c" ] && [ "$2" = "%Y" ]; then {mtime}; fi\n'
                   'if [ "$1" = "-f" ]; then printf \'  File: "%s"\\n    ID: 0 Namelen: 255\\n\' "$3"; '
                   'echo "stat: cannot read file system information for \'$2\'" >&2; exit 1; fi\nexit 1',
            "bsd": f'if [ "$1" = "-f" ] && [ "$2" = "%m" ]; then {mtime}; fi\n'
                   'echo "stat: illegal option -- ${1#-}" >&2; exit 1',
            "garbage": 'echo "not-a-number"; exit 0',
        }
        stat = shim_dir / "stat"
        stat.write_text("#!/usr/bin/env bash\n" + bodies[flavor] + "\n")
        os.chmod(stat, 0o755)
        return shim_dir

    def test_r17_portable_marker_mtime(self):
        """R17 (supports Plan §10.1 #23): marker mtime works with GNU and BSD stat and rejects non-numeric output."""
        script = write_guard_script(self.state_dir / "test-guard-stat.sh", 'echo "PASSTHROUGH"')
        pane = "w1:pStatPortable"
        marker = self.state_dir / "panes" / get_hex_pane_id(pane)
        for flavor, expect_suppressed in (("gnu", True), ("bsd", True), ("garbage", False)):
            with self.subTest(stat=flavor):
                marker.write_text(str(int(time.time())))
                env = {"HERDR_PANE_ID": pane, "PATH": f"{self._stat_shim_dir(flavor)}{os.pathsep}{os.environ['PATH']}"}
                res = run_guard(script, "Working", env_extra=env)
                self.assertEqual(res.returncode, 0, res.stderr)
                if expect_suppressed:
                    self.assertNotIn("PASSTHROUGH", res.stdout, "fresh marker + healthy herdr must suppress")
                else:
                    self.assertIn("PASSTHROUGH", res.stdout, "unparseable mtime must fall back to 0 and fall through")
                va = self.state_dir / "panes" / f"{get_hex_pane_id(pane)}.vendor_active"
                va.unlink(missing_ok=True)

    # WEAK: t32-bypass
    def test_p32_large_stdin_byte_exact(self):
        """Plan §10.1 #32: >64KB stdin reaches the vendor body byte-exact."""
        script = write_guard_script(self.state_dir / "test-guard-large.sh", "cat")
        large_payload = b"X" * 131072 + b"\n" + b"Y" * 65536
        res_large = run_guard(script, input=large_payload, env_extra={"HERDR_PANE_ID": "unconfigured_pane_bypass"}, text=False)
        self.assertEqual(res_large.stdout, large_payload,
                         f"Large stdin payload must be byte-exact, got {len(res_large.stdout)} bytes vs {len(large_payload)}")

    # WEAK: t33-terminal-matrix
    def test_p33_turn_vs_session_terminal(self):
        """Plan §10.1 #33: Done passes through retaining .vendor_active; Ended passes through clearing it."""
        script = write_guard_script(self.state_dir / "test-guard-term.sh", 'echo "PASSTHROUGH"')
        t_hex = get_hex_pane_id("default:pTermTest")
        t_active = self.state_dir / "panes" / f"{t_hex}.vendor_active"
        t_marker = self.state_dir / "panes" / t_hex
        env = {"HERDR_PANE_ID": "default:pTermTest"}

        t_active.touch()
        res = run_guard(script, "Done", env_extra=env)
        self.assertTrue(res.returncode == 0 and "PASSTHROUGH" in res.stdout)
        self.assertTrue(t_active.exists(), "Turn-terminal Done must retain .vendor_active")

        res = run_guard(script, "Ended", env_extra=env)
        self.assertTrue(res.returncode == 0 and "PASSTHROUGH" in res.stdout)
        self.assertFalse(t_active.exists(), "Session-terminal Ended must clear .vendor_active")

        t_marker.unlink(missing_ok=True)
        res = run_guard(script, "Ended", env_extra=env)
        self.assertIn("PASSTHROUGH", res.stdout, "Terminal event must fall through if Herdr ownership predicate fails")

    # WEAK: t41-failopen-hollow
    def test_p41_fail_open_on_mktemp_failure(self):
        """Plan §10.1 #41: the guard fails open (stdin intact) when mktemp fails."""
        guard_code = HOOK_GUARD_TEMPLATE.replace('mktemp "$STATE_HOME/.guard_stdin.XXXXXX"', 'false')
        script = write_guard_script(self.state_dir / "test-guard-failopen.sh", 'echo "PASSTHROUGH_DATA:$(cat)"', guard=guard_code)
        res_fo = run_guard(script, input="ImportantStdinData", env_extra={"HERDR_PANE_ID": "pFailOpen"})
        self.assertEqual(res_fo.returncode, 0)
        self.assertIn("PASSTHROUGH_DATA:ImportantStdinData", res_fo.stdout, "Guard must fail-open to vendor hook when mktemp fails")

    # WEAK: t46-56-bounds
    def test_p46_bounded_capture_on_never_closing_fifo(self):
        """Plan §10.1 #46: a stalled, never-closing stdin FIFO is bounded and the guard fails open."""
        script = write_guard_script(self.state_dir / "test-guard-fifo.sh", 'echo "PASSTHROUGH_SUCCESS"')
        fifo_path = self.state_dir / "test_hanging_fifo"
        os.mkfifo(str(fifo_path))
        self._fifo_writer(fifo_path, f"exec 3>'{fifo_path}'; sleep 3; exec 3>&-")
        t_start = time.time()
        with open(str(fifo_path), "r") as r_fifo:
            res_fifo = run_guard(script, stdin=r_fifo, env_extra={"HERDR_PANE_ID": "w1:pFifoTest"})
        t_elapsed = time.time() - t_start
        self.assertIn("PASSTHROUGH_SUCCESS", res_fifo.stdout, "Guard must pass through on hanging FIFO")
        self.assertLess(t_elapsed, 2.5, f"Guard must time out boundedly (took {t_elapsed:.2f}s)")

    def test_p52_session_terminal_passes_without_vendor_active(self):
        """Plan §10.1 #52: with a healthy marker and no .vendor_active, session-terminal Ended still passes through."""
        pane_52 = "w1:pPassTerminal"
        hex_52 = get_hex_pane_id(pane_52)
        (self.state_dir / "panes" / hex_52).write_text(str(int(time.time())))
        (self.state_dir / "panes" / f"{hex_52}.vendor_active").unlink(missing_ok=True)
        script = self._exported_guard("test-guard-52.sh", pane_52, 'echo "PASSTHROUGH_TERMINAL"')
        res_term = run_guard(script, "Ended")
        self.assertIn("PASSTHROUGH_TERMINAL", res_term.stdout, "Session-terminal Ended must pass through even when .vendor_active is absent")

    def test_p55_bare_touch_pass_through_and_recovery(self):
        """Plan §10.1 #55: turn-terminal events pass under a bare touch; healthy non-terminal suppresses and unlinks it."""
        pane_55 = "w1:pBarePass"
        hex_55 = get_hex_pane_id(pane_55)
        (self.state_dir / "panes" / hex_55).write_text(str(int(time.time())))
        va_55 = self.state_dir / "panes" / f"{hex_55}.vendor_active"
        va_55.write_text("")
        script = self._exported_guard("test-guard-55.sh", pane_55, 'echo "PASSTHROUGH_BARE"')

        res_bare = run_guard(script, "Stop")
        self.assertIn("PASSTHROUGH_BARE", res_bare.stdout, "Bare touch must never suppress turn-terminal vendor events")
        self.assertTrue(va_55.exists(), "Turn-terminal event must not unlink bare touch")

        res_suppress = run_guard(script, "Working")
        self.assertEqual(res_suppress.returncode, 0)
        self.assertNotIn("PASSTHROUGH_BARE", res_suppress.stdout, "Healthy Herdr must suppress non-terminal event under bare touch")
        self.assertFalse(va_55.exists(), "Healthy Herdr must unlink bare touch to restore dedup")

    # WEAK: t46-56-bounds
    def test_p56_bounded_capture_with_splice(self):
        """Plan §10.1 #56: a stalled pipe is captured within bounds and the prefix is spliced to the vendor body."""
        script = self._exported_guard("test-guard-56.sh", "w1:pStdinPipe", "head -n 1")
        fifo_56 = self.state_dir / "test_hanging_fifo_56"
        os.mkfifo(str(fifo_56))
        self._fifo_writer(fifo_56, f"exec 3>'{fifo_56}'; echo '{{\"session_id\":\"1234567890123456\"}}' >&3; sleep 4; exec 3>&-")
        t_start = time.time()
        with open(str(fifo_56), "r") as r_fifo:
            res_56 = run_guard(script, stdin=r_fifo, env_extra={"HERDR_PANE_ID": "w1:pStdinPipe"})
        t_elapsed = time.time() - t_start
        self.assertLess(t_elapsed, 2.0, f"Hook guard stdin capture must be strictly bounded (<2.0s), took {t_elapsed}s")
        self.assertIn('{"session_id":"1234567890123456"}', res_56.stdout, "Captured stdin prefix must be preserved and spliced to vendor cat")

    def test_p61_codex_argv_json(self):
        """Plan §10.1 #61: argv JSON payloads are classified turn-/session-terminal/non-terminal correctly."""
        pane_61 = "w1:pCodexArgv"
        va_61 = self.state_dir / "panes" / f"{get_hex_pane_id(pane_61)}.vendor_active"
        va_61.write_text("")
        script = self._exported_guard("test-guard-61.sh", pane_61, 'echo "PASSTHROUGH_61"')

        res_argv_turn = run_guard(script, '{"session_id":"test_codex_sid_12345","hook_event_name":"agent-turn-complete"}')
        self.assertIn("PASSTHROUGH_61", res_argv_turn.stdout, "Turn-terminal event in argv JSON must pass through")
        self.assertTrue(va_61.exists(), "Turn-terminal event must not unlink bare touch")

        res_argv_sess = run_guard(script, '{"session_id":"test_codex_sid_12345","hook_event_name":"session-end"}')
        self.assertIn("PASSTHROUGH_61", res_argv_sess.stdout, "Session-terminal event in argv JSON must pass through")
        self.assertFalse(va_61.exists(), "Session-terminal event must unlink vendor_active")

        touch_pane_marker(pane_61)
        res_argv_suppress = run_guard(script, '{"session_id":"test_codex_sid_12345","hook_event_name":"agent-busy"}')
        self.assertEqual(res_argv_suppress.returncode, 0)
        self.assertNotIn("PASSTHROUGH_61", res_argv_suppress.stdout, "Healthy Herdr must suppress non-terminal argv JSON event")
        remove_pane_marker(pane_61)

    # WEAK: t1-19-57-62-minor
    def test_p62_fail_open_on_empty_stdin_without_argv(self):
        """Plan §10.1 #62: empty stdin and no argv token/JSON fails open to the vendor hook."""
        pane_62 = "w1:pEmptyTty"
        script = self._exported_guard("test-guard-62.sh", pane_62, 'echo "PASSTHROUGH_62"')
        touch_pane_marker(pane_62)
        res_empty_pipe = run_guard(script, stdin=subprocess.DEVNULL)
        self.assertEqual(res_empty_pipe.returncode, 0)
        self.assertIn("PASSTHROUGH_62", res_empty_pipe.stdout, "Empty stdin without argv must fail-open")
        remove_pane_marker(pane_62)

    def test_p67_turn_terminal_under_uuid_record(self):
        """Plan §10.1 #67: turn-terminal events pass under a UUID record (kept); non-terminal is suppressed."""
        pane_67 = "w1:pTurnPassUUID"
        script = self._exported_guard("test-guard-67.sh", pane_67, 'echo "PASSTHROUGH_67"')
        touch_pane_marker(pane_67)
        va_67 = self.state_dir / "panes" / f"{get_hex_pane_id(pane_67)}.vendor_active"
        va_67.write_text('{"vendor_session_id":"test_uuid_turn_67"}')

        res_turn_67 = run_guard(script, "Stop")
        self.assertEqual(res_turn_67.returncode, 0)
        self.assertIn("PASSTHROUGH_67", res_turn_67.stdout, "Turn-terminal event under UUID record must pass through")
        self.assertTrue(va_67.exists(), ".vendor_active must be retained after turn-terminal pass-through")

        res_non_turn_67 = run_guard(script, "Working")
        self.assertEqual(res_non_turn_67.returncode, 0)
        self.assertNotIn("PASSTHROUGH_67", res_non_turn_67.stdout, "Non-terminal event under healthy Herdr must be suppressed")


if __name__ == "__main__":
    unittest.main()
