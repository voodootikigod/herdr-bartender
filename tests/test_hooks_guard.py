"""Vendor-hook dedup guard (hook_guard.sh) behavior.

Herdr liveness inside the guard is decided by the sandbox pgrep shim; setUp
registers a fake running Herdr so "healthy" paths are reachable on any host.
"""

import json
import os
import shutil
import subprocess
import time
import unittest

from herdr_bartender.hooks import HOOK_GUARD_TEMPLATE
from herdr_bartender.markers import remove_pane_marker, touch_pane_marker
from herdr_bartender.sanitize import get_hex_pane_id, normalize_pane_id
from tests.support import SandboxTestCase, run_guard, write_guard_script
from tests.support.guard_harness import (
    failing_mktemp_env,
    file_mode,
    leftovers,
    make_shim,
    path_with,
    run_guard_with_umask,
    start_fifo_writer,
)

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
        shim_dir.mkdir(exist_ok=True)
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

    def _failing_date_dir(self):
        shim_dir = self.tmp / "date-fails"
        make_shim(shim_dir, "date", 'echo "date: simulated failure" >&2\nexit 1')
        return shim_dir

    def test_r17_portable_marker_mtime(self):
        """R17 (supports Plan §10.1 #23, gap stat-order-aborts-guard): marker mtime works with GNU and BSD stat;
        unparseable mtime, a failing `date`, or a future (clock-skewed) mtime all fail open (pass through)."""
        script = write_guard_script(self.state_dir / "test-guard-stat.sh", 'echo "PASSTHROUGH"')
        pane = "w1:pStatPortable"
        marker = self.state_dir / "panes" / get_hex_pane_id(pane)
        cases = (
            ("gnu", ("gnu",), 0, True),
            ("bsd", ("bsd",), 0, True),
            ("garbage", ("garbage",), 0, False),
            ("date-fails", ("gnu", "date-fails"), 0, False),
            ("future-mtime", ("gnu",), 3600, False),
        )
        for label, shims, mtime_offset, expect_suppressed in cases:
            with self.subTest(case=label):
                marker.write_text(str(int(time.time())))
                future = int(time.time()) + mtime_offset
                os.utime(marker, (future, future))
                dirs = [self._failing_date_dir() if s == "date-fails" else self._stat_shim_dir(s) for s in shims]
                env = {"HERDR_PANE_ID": pane, "PATH": os.pathsep.join([*map(str, dirs), os.environ["PATH"]])}
                res = run_guard(script, "Working", env_extra=env)
                self.assertEqual(res.returncode, 0, res.stderr)
                if expect_suppressed:
                    self.assertNotIn("PASSTHROUGH", res.stdout, "fresh marker + healthy herdr must suppress")
                else:
                    self.assertIn("PASSTHROUGH", res.stdout, f"{label}: marker freshness unknown/invalid must fall through")
                va = self.state_dir / "panes" / f"{get_hex_pane_id(pane)}.vendor_active"
                va.unlink(missing_ok=True)

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


TURN_TERMINAL = ("Stop", "Done", "AgentDone", "AgentWaiting", "agent-turn-complete")
SESSION_TERMINAL = ("Ended", "SessionEnd", "session-end")
DELIVERY_MODES = ("argv-token", "stdin-json", "argv-json")
SID = "matrix_session_id_0001"


class _GuardCase(SandboxTestCase):
    """Shared helpers: canonical pane paths, fresh markers, and guard scripts."""

    start_bridge = False

    def setUp(self):
        super().setUp()
        self.herdr_pid = self.set_herdr_alive()
        self.panes = self.state_dir / "panes"
        self.panes.mkdir(parents=True, exist_ok=True)
        self.shim_bin = self.tmp / "guard-bin"

    def paths(self, pane):
        hex_id = get_hex_pane_id(pane)
        return self.panes / hex_id, self.panes / f"{hex_id}.vendor_active"

    def fresh_marker(self, pane):
        marker, _ = self.paths(pane)
        marker.write_text(str(int(time.time())))
        return marker

    def script(self, name, tail, shebang="#!/bin/bash\nset -u"):
        return write_guard_script(self.tmp / name, tail, shebang=shebang)

    def set_herdr_dead(self):
        self.clear_fake_processes()

    def invoke(self, script, pane, event, mode, **kwargs):
        env = {"HERDR_PANE_ID": pane, **kwargs.pop("env_extra", {})}
        if mode == "argv-token":
            return run_guard(script, event, env_extra=env, **kwargs)
        payload = json.dumps({"session_id": SID, "hook_event_name": event})
        if mode == "argv-json":
            return run_guard(script, payload, env_extra=env, **kwargs)
        return run_guard(script, input=payload, env_extra=env, **kwargs)


class HookGuardCaptureTests(_GuardCase):
    """Stdin capture: byte-exactness, fail-open and timing bounds."""

    PAYLOADS = {
        "trailing-newline": b"X" * 131072 + b"\n" + b"Y" * 65536 + b"\n",
        "no-trailing-newline": b"X" * 131072 + b"\n" + b"Y" * 65536,
        "binary-nul": bytes(range(256)) * 768 + b"\x00\x00tail",
    }

    def test_p32_large_stdin_byte_exact_fall_through(self):
        """Plan §10.1 #32 (gap t32-bypass): >64KB stdin on a canonical pane reaches the vendor byte-exact."""
        script = self.script("guard-large.sh", "cat")
        for label, payload in self.PAYLOADS.items():
            with self.subTest(payload=label):
                self.assertGreater(len(payload), 65536)
                res = run_guard(script, input=payload, env_extra={"HERDR_PANE_ID": "w1:pLarge"}, text=False)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(res.stdout, payload, f"got {len(res.stdout)} bytes, want {len(payload)}")
                self.assertTrue(self.paths("w1:pLarge")[1].exists(), "capture path ran (fall-through records .vendor_active)")
                self.assertEqual(leftovers(self.state_dir, ".guard_stdin.*", ".guard_splice.*"), [])

    def test_p32_large_stdin_healthy_suppress_leaves_no_capture_file(self):
        """Plan §10.1 #32 (gap t32-bypass): the healthy-suppress path consumes >64KB stdin and leaves no .guard_stdin.*."""
        script = self.script("guard-large-suppress.sh", "cat")
        self.fresh_marker("w1:pLargeOwned")
        res = run_guard(script, input=self.PAYLOADS["binary-nul"], env_extra={"HERDR_PANE_ID": "w1:pLargeOwned"}, text=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, b"", "healthy Herdr must suppress the vendor body")
        self.assertEqual(leftovers(self.state_dir, ".guard_stdin.*", ".guard_splice.*"), [])

    def test_p41_fail_open_when_mktemp_fails(self):
        """Plan §10.1 #41 (gap t41-failopen-hollow): mktemp failure fails open with stdin intact and no unlinking."""
        pane = "w1:pFailOpen"
        self.fresh_marker(pane)
        _, va = self.paths(pane)
        va.write_text("")
        script = self.script("guard-failopen.sh", 'printf "PASSTHROUGH_DATA:"; cat')
        payload = "ImportantStdinData\nline2 with \"quotes\"\n"
        res = run_guard(script, "Working", input=payload,
                        env_extra={"HERDR_PANE_ID": pane, **failing_mktemp_env(self.shim_bin)})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "PASSTHROUGH_DATA:" + payload, "vendor must receive the full stdin byte-for-byte")
        self.assertTrue(va.exists(), "fail-open must not unlink an existing .vendor_active")
        self.assertEqual(leftovers(self.state_dir, ".guard_stdin.*", ".guard_splice.*", ".va.tmp.*"), [])

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_p41_fail_open_on_read_only_state_dir(self):
        """Plan §10.1 #41 (gap t41-failopen-hollow): an unwritable state dir fails open with stdin intact."""
        pane = "w1:pReadOnly"
        self.fresh_marker(pane)
        os.chmod(self.state_dir, 0o500)
        self.addCleanup(os.chmod, self.state_dir, 0o700)
        script = self.script("guard-ro.sh", 'printf "PASSTHROUGH_DATA:"; cat')
        res = run_guard(script, input="ReadOnlyStdin", env_extra={"HERDR_PANE_ID": pane})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "PASSTHROUGH_DATA:ReadOnlyStdin")

    def test_vendor_stdio_survives_capture_and_splice(self):
        """Plan §7.1 (stdin spooling): after capture or splice the vendor body keeps its stderr and stdout."""
        script = self.script("guard-stdio.sh", 'cat; echo "VENDOR_STDERR" >&2')
        res = run_guard(script, input="captured", env_extra={"HERDR_PANE_ID": "w1:pStdio"})
        self.assertEqual((res.returncode, res.stdout), (0, "captured"))
        self.assertIn("VENDOR_STDERR", res.stderr, "the guard must not redirect the vendor's stderr")
        fifo = self.tmp / "stdio_fifo"
        os.mkfifo(str(fifo))
        start_fifo_writer(self, fifo, f"exec 3>'{fifo}'; printf 'a' >&3; sleep 1.3; printf 'b' >&3; exec 3>&-")
        with open(str(fifo), "rb") as reader:
            res = run_guard(script, stdin=reader, env_extra={"HERDR_PANE_ID": "w1:pStdio"})
        self.assertEqual((res.returncode, res.stdout), (0, "ab"))
        self.assertIn("VENDOR_STDERR", res.stderr, "the splice path must not redirect the vendor's stderr")

    def test_p46_never_closing_fifo_is_bounded(self):
        """Plan §10.1 #46 (gap t46-56-bounds): a never-closing stdin FIFO is abandoned within ~1.0s and fails open."""
        pane = "w1:pFifoTest"
        self.fresh_marker(pane)
        script = self.script("guard-fifo.sh", 'echo "PASSTHROUGH_SUCCESS"')
        fifo = self.tmp / "hanging_fifo"
        os.mkfifo(str(fifo))
        start_fifo_writer(self, fifo, f"exec 3>'{fifo}'; sleep 5; exec 3>&-")
        with open(str(fifo), "rb") as reader:
            t_start = time.monotonic()
            res = run_guard(script, stdin=reader, env_extra={"HERDR_PANE_ID": pane})
            elapsed = time.monotonic() - t_start
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("PASSTHROUGH_SUCCESS", res.stdout, "capture timeout must fail open (no suppression)")
        self.assertLess(elapsed, 1.5, f"capture must be bounded to <=1.0s (+tolerance), took {elapsed:.2f}s")
        self.assertEqual(leftovers(self.state_dir, ".guard_stdin.*", ".guard_splice.*"), [])

    def _splice_shells(self):
        shells = [("bash", "#!/bin/bash\nset -u")]
        dash = shutil.which("dash")
        if dash:
            shells.append(("dash", f"#!{dash}\nset -u"))
        return shells

    def test_p56_stalled_pipe_splices_prefix_and_remainder(self):
        """Plan §10.1 #56 (gap t46-56-bounds): captured prefix + late remainder reach the vendor byte-exact in <2.0s,
        under bash and (when installed) dash, since the installer accepts `sh` shebangs (Linux /bin/sh is dash)."""
        line1 = '{"session_id":"1234567890123456","hook_event_name":"UserPromptSubmit"}\n'
        line2 = "remainder-line-after-alarm\n"
        for shell, shebang in self._splice_shells():
            with self.subTest(shell=shell):
                pane = f"w1:pStdinPipe{shell}"
                self.fresh_marker(pane)
                script = self.script(f"guard-splice-{shell}.sh", "cat", shebang=shebang)
                fifo = self.tmp / f"splice_fifo_{shell}"
                os.mkfifo(str(fifo))
                start_fifo_writer(self, fifo, f"exec 3>'{fifo}'; printf '%s' '{line1}' >&3; sleep 1.5; printf '%s' '{line2}' >&3; exec 3>&-")
                with open(str(fifo), "rb") as reader:
                    t_start = time.monotonic()
                    res = run_guard(script, stdin=reader, env_extra={"HERDR_PANE_ID": pane})
                    elapsed = time.monotonic() - t_start
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(res.stdout, line1 + line2, "splice must deliver captured prefix followed by the remainder")
                self.assertLess(elapsed, 2.0, f"guard must hand control back within 2.0s, took {elapsed:.2f}s")
                self.assertEqual(leftovers(self.state_dir, ".guard_stdin.*", ".guard_splice.*"), [])


class HookGuardMatrixTests(_GuardCase):
    """Terminal-event classification across delivery modes and Herdr health."""

    def _run_matrix(self, events, expect_va_kept):
        script = self.script("guard-matrix.sh", 'echo "PASSTHROUGH"')
        for healthy in (True, False):
            if not healthy:
                self.set_herdr_dead()
            for event in events:
                for mode in DELIVERY_MODES:
                    with self.subTest(event=event, mode=mode, healthy=healthy):
                        pane = f"w1:p{abs(hash((event, mode, healthy))) % 10**8}"
                        self.fresh_marker(pane)
                        _, va = self.paths(pane)
                        va.write_text("")
                        res = self.invoke(script, pane, event, mode)
                        self.assertEqual(res.returncode, 0, res.stderr)
                        self.assertIn("PASSTHROUGH", res.stdout, "terminal events always pass through")
                        self.assertEqual(va.exists(), expect_va_kept)

    def test_p33_turn_terminal_matrix_retains_vendor_active(self):
        """Plan §10.1 #33/#23 (gap t33-terminal-matrix): turn-terminal events pass through and keep .vendor_active."""
        self._run_matrix(TURN_TERMINAL, expect_va_kept=True)

    def test_p33_session_terminal_matrix_unlinks_vendor_active(self):
        """Plan §10.1 #33/#23 (gap t33-terminal-matrix): session-terminal events pass through and unlink .vendor_active."""
        self._run_matrix(SESSION_TERMINAL, expect_va_kept=False)

    def test_p45_guard_never_expires_aged_vendor_active(self):
        """Plan §10.1 #45 (gap t45-bare-60s, guard side): a 120s-old bare touch is kept and refreshed, never expired."""
        script = self.script("guard-aged.sh", 'echo "PASSTHROUGH"')
        pane = "w1:pAgedBare"
        _, va = self.paths(pane)
        va.write_text("")
        aged = time.time() - 120
        os.utime(va, (aged, aged))
        self.set_herdr_dead()
        res = run_guard(script, "Working", env_extra={"HERDR_PANE_ID": pane})
        self.assertIn("PASSTHROUGH", res.stdout)
        self.assertTrue(va.exists(), "an aged bare touch must not be blindly expired")
        self.assertGreater(os.stat(va).st_mtime, aged + 60, "pass-through must refresh .vendor_active")
        self.assertEqual(va.read_text(), "", "bare touch stays bare without a session id")

        self.set_herdr_alive()
        self.fresh_marker(pane)
        os.utime(va, (aged, aged))
        res = run_guard(script, "Stop", env_extra={"HERDR_PANE_ID": pane})
        self.assertIn("PASSTHROUGH", res.stdout, "turn-terminal under an aged bare touch passes through")
        self.assertTrue(va.exists())


    def test_r17_bare_touch_upgraded_to_uuid_record(self):
        """R17 / Plan §7.2 (gap plan-prose-vs-block-minor): a bare touch is upgraded once a session id is seen."""
        script = self.script("guard-upgrade.sh", 'echo "PASSTHROUGH"')
        pane = "w1:pUpgrade"
        _, va = self.paths(pane)
        va.write_text("")
        self.set_herdr_dead()
        res = self.invoke(script, pane, "UserPromptSubmit", "stdin-json")
        self.assertIn("PASSTHROUGH", res.stdout)
        self.assertEqual(json.loads(va.read_text()), {"vendor_session_id": SID})
        self.assertEqual(leftovers(self.state_dir, ".va.tmp.*"), [])


class HookGuardHardeningTests(_GuardCase):
    """R15 liveness probe, R16 umask, strict-mode callers, syntax."""

    def test_r15_herdr_liveness_probe(self):
        """R15 (gaps herdr-liveness-pgrep-f, portability-guard-herdr-liveness): only a real herdr process counts."""
        script = self.script("guard-liveness.sh", 'echo "PASSTHROUGH"')
        cases = (
            ("no-process", None, True),
            ("cmdline-mentions-bundle", ("vim", "vim /Applications/Herdr.app/Contents/Info.plist"), True),
            ("tail-of-bundle-log", ("tail", "tail -f /Users/x/Herdr.app/Contents/MacOS/log.txt"), True),
            ("exact-name-case-insensitive", ("Herdr", "Herdr"), False),
            ("bundle-executable", ("Herdr-GUI", "/Applications/Herdr.app/Contents/MacOS/Herdr-GUI --flag"), False),
        )
        for label, proc, expect_pass in cases:
            with self.subTest(case=label):
                self.clear_fake_processes()
                if proc:
                    self.add_fake_process(proc[0], pid=40000 + len(label), comm=proc[0], cmdline=proc[1])
                pane = f"w1:pLive{len(label)}"
                self.fresh_marker(pane)
                res = run_guard(script, "Working", env_extra={"HERDR_PANE_ID": pane})
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual("PASSTHROUGH" in res.stdout, expect_pass)

    def test_herdr_running_as_the_hooks_ancestor_is_seen(self):
        """Round-3 finding (critical): macOS (BSD) pgrep drops the caller and ALL its ancestors from the match list
        unless -a is given, and Herdr is an ancestor of every vendor hook (Herdr -> pane shell -> agent CLI -> hook ->
        pgrep). Without -a neither probe ever saw Herdr, so the guard never suppressed and every agent showed twice.
        The pgrep shim models BSD ancestry; both probes must still find an ancestor Herdr."""
        script = self.script("guard-ancestor.sh", 'echo "PASSTHROUGH"')
        cases = (("name", "herdr", "herdr server"),
                 ("bundle", "Herdr-GUI", "/Applications/Herdr.app/Contents/MacOS/Herdr-GUI --flag"))
        for label, name, cmdline in cases:
            with self.subTest(probe=label):
                self.clear_fake_processes()
                self.add_fake_process(name, pid=41000 + len(label), comm=name, cmdline=cmdline, ancestor=True)
                pane = f"w1:pAncestor{label}"
                self.fresh_marker(pane)
                res = run_guard(script, "Working", env_extra={"HERDR_PANE_ID": pane})
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertNotIn("PASSTHROUGH", res.stdout, "Herdr (an ancestor) is alive: the event is suppressed")
                self.assertFalse(self.paths(pane)[1].exists(), "no .vendor_active for a suppressed event")

    def test_r15_pgrep_failure_fails_open(self):
        """R15 (gap portability-guard-herdr-liveness): a failing pgrep means Herdr is not proven alive -> pass through."""
        (self.sandbox / "pgrep.fail").write_text("")
        script = self.script("guard-pgrep-fail.sh", 'echo "PASSTHROUGH"')
        self.fresh_marker("w1:pPgrepFail")
        res = run_guard(script, "Working", env_extra={"HERDR_PANE_ID": "w1:pPgrepFail"})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("PASSTHROUGH", res.stdout)

    def test_r16_guard_writes_under_umask_077_and_restores(self):
        """R16 / Plan §8 (gap vendor-active-perms): guard files are 0600 and the vendor sees the caller umask."""
        script = self.script("guard-umask.sh", 'echo "UMASK=$(umask)"')
        self.set_herdr_dead()
        cases = (
            ("bare-touch", "w1:pUmaskBare", ("Working",), {}),
            ("uuid-mktemp", "w1:pUmaskUuid", (json.dumps({"session_id": SID, "hook_event_name": "X"}),), {}),
            ("uuid-printf-fallback", "w1:pUmaskFb", (json.dumps({"session_id": SID, "hook_event_name": "X"}),),
             failing_mktemp_env(self.shim_bin)),
        )
        for label, pane, args, env in cases:
            with self.subTest(case=label):
                res = run_guard_with_umask(script, "022", *args, env_extra={"HERDR_PANE_ID": pane, **env})
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn("UMASK=0022", res.stdout, "caller umask must be restored before the vendor body")
                _, va = self.paths(pane)
                self.assertTrue(va.exists())
                self.assertEqual(file_mode(va), 0o600, f"{label}: .vendor_active must be 0600")

    def test_r16_creates_missing_panes_dir_0700(self):
        """R16 / Plan §8 (gap vendor-active-perms): a missing panes dir is created 0700 under umask 022."""
        self.panes.rmdir()
        script = self.script("guard-panes-dir.sh", 'echo "PASSTHROUGH"')
        res = run_guard_with_umask(script, "022", "Working", env_extra={"HERDR_PANE_ID": "w1:pNewDir"})
        self.assertIn("PASSTHROUGH", res.stdout)
        self.assertEqual(file_mode(self.panes), 0o700)
        self.assertEqual(file_mode(self.paths("w1:pNewDir")[1]), 0o600)

    def test_guard_safe_under_strict_caller_options(self):
        """Plan §7.2 (gap stat-order-aborts-guard): the guard never aborts a `set -euo pipefail` vendor script."""
        strict = "#!/bin/bash\nset -euo pipefail"
        script = self.script("guard-strict.sh", 'printf "PASSTHROUGH:"; cat', shebang=strict)
        owned = "w1:pStrictOwned"
        self.fresh_marker(owned)
        payload = json.dumps({"session_id": SID, "hook_event_name": "UserPromptSubmit"})
        checks = (
            ("fall-through", "w1:pStrictFree", (), payload, {}, "PASSTHROUGH:" + payload),
            ("suppress", owned, (), payload, {}, ""),
            ("session-terminal", owned, ("Ended",), "", {}, "PASSTHROUGH:"),
            ("mktemp-fails", owned, (), payload, failing_mktemp_env(self.shim_bin), "PASSTHROUGH:" + payload),
            ("stat-fails", owned, ("Working",), "x", {"PATH": path_with(self._broken_stat())}, "PASSTHROUGH:x"),
        )
        for label, pane, args, stdin, env, expected in checks:
            with self.subTest(case=label):
                res = run_guard(script, *args, input=stdin, env_extra={"HERDR_PANE_ID": pane, **env})
                self.assertEqual(res.returncode, 0, f"{label}: {res.stderr}")
                self.assertEqual(res.stdout, expected)

    def _broken_stat(self):
        bin_dir = self.tmp / "broken-stat"
        make_shim(bin_dir, "stat", "exit 1")
        return bin_dir

    def test_guard_template_syntax_and_markers(self):
        """Plan §7.2: hook_guard.sh is `bash -n` (and `dash -n`, gap t46-56-bounds) clean with exactly one BEGIN and one END marker."""
        guard_path = self.tmp / "guard-only.sh"
        guard_path.write_text(HOOK_GUARD_TEMPLATE + "\n")
        for shell in filter(None, ("bash", shutil.which("dash"))):
            res = subprocess.run([shell, "-n", str(guard_path)], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"{shell}: {res.stderr}")
        self.assertEqual(HOOK_GUARD_TEMPLATE.count("# BEGIN HERDR-BARTENDER DEDUP GUARD"), 1)
        self.assertEqual(HOOK_GUARD_TEMPLATE.count("# END HERDR-BARTENDER DEDUP GUARD"), 1)
        self.assertTrue(HOOK_GUARD_TEMPLATE.endswith("# END HERDR-BARTENDER DEDUP GUARD"))
        self.assertFalse('pgrep -f "Herdr.app"' in HOOK_GUARD_TEMPLATE, "R15: loose bundle match is forbidden")


if __name__ == "__main__":
    unittest.main()
