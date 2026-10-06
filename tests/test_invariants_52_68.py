"""Plan §10.1 invariants #52, #61, #62 and #67: strengthened hook-guard coverage.

Every test drives the real production guard block (herdr_bartender/hook_guard.sh via
HOOK_GUARD_TEMPLATE) as a subprocess inside the sandbox. Herdr liveness comes from
the pgrep shim's fake process table, so "healthy" never depends on the host.
"""

import json
import os
import pty
import shutil
import time

from herdr_bartender.sanitize import get_hex_pane_id
from tests.support import SandboxTestCase, run_guard, write_guard_script

TURN_TERMINAL = ("Stop", "Done", "AgentDone", "AgentWaiting", "agent-turn-complete")
SESSION_TERMINAL = ("Ended", "SessionEnd", "session-end")
DELIVERY_MODES = ("argv-token", "stdin-json", "argv-json")
PAYLOAD_SID = "payload_session_id_0001"
# Vendor body: echo a sentinel, then whatever stdin the guard handed over.
ECHO_STDIN_TAIL = 'printf "PASSTHROUGH:"; cat'


class _InvariantGuardCase(SandboxTestCase):
    """Canonical pane paths, fresh markers and a uniform 3-mode event delivery."""

    start_bridge = False

    def setUp(self):
        super().setUp()
        self.panes = self.state_dir / "panes"
        self.panes.mkdir(parents=True, exist_ok=True)
        self._pane_seq = 0

    def new_pane(self, tag):
        self._pane_seq += 1
        return f"w1:p{tag}{self._pane_seq}"

    def paths(self, pane):
        hex_id = get_hex_pane_id(pane)
        return self.panes / hex_id, self.panes / f"{hex_id}.vendor_active"

    def fresh_marker(self, pane):
        marker, _ = self.paths(pane)
        marker.write_text(str(int(time.time())))
        return marker

    def script(self, name, tail=ECHO_STDIN_TAIL, shebang="#!/bin/bash\nset -u"):
        return write_guard_script(self.tmp / name, tail, shebang=shebang)

    def deliver(self, script, pane, event, mode, sid=PAYLOAD_SID):
        """Run the guard for ``event`` delivered as an argv token, stdin JSON or argv JSON.

        Returns (completed_process, stdin_bytes_sent_as_text).
        """
        env = {"HERDR_PANE_ID": pane}
        if mode == "argv-token":
            return run_guard(script, event, env_extra=env), ""
        payload = json.dumps({"session_id": sid, "hook_event_name": event})
        if mode == "argv-json":
            return run_guard(script, payload, env_extra=env), ""
        return run_guard(script, input=payload, env_extra=env), payload

    def assert_passthrough(self, res, stdin_text):
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "PASSTHROUGH:" + stdin_text,
                         "the vendor body must run and receive the original stdin byte-exact")


class SessionTerminalWithoutVendorActiveTests(_InvariantGuardCase):
    """Plan §10.1 #52."""

    def test_p52_session_terminal_passes_through_every_mode_without_vendor_active(self):
        """Plan §10.1 #52: fresh marker + Herdr alive + no .vendor_active: Ended/SessionEnd/session-end
        pass through in argv-token, stdin-JSON and argv-JSON modes, and no .vendor_active is created."""
        script = self.script("guard-52.sh")
        for event in SESSION_TERMINAL:
            for mode in DELIVERY_MODES:
                with self.subTest(event=event, mode=mode):
                    pane = self.new_pane("SessTerm")
                    self.fresh_marker(pane)
                    _, va = self.paths(pane)
                    self.assertFalse(va.exists())
                    res, sent = self.deliver(script, pane, event, mode)
                    self.assert_passthrough(res, sent)
                    self.assertFalse(va.exists(), "a session-terminal event must never create .vendor_active")
                    self.assertEqual(sorted(p.name for p in self.state_dir.rglob(".guard_*")), [],
                                     "no capture/splice temp files may be left behind")

    def test_p52_healthy_control_non_terminal_is_suppressed(self):
        """Plan §10.1 #52 (control): the same healthy pane does suppress a non-terminal event in every mode,
        proving the session-terminal pass-through is not merely an unhealthy-pane artifact."""
        script = self.script("guard-52-control.sh")
        for mode in DELIVERY_MODES:
            with self.subTest(mode=mode):
                pane = self.new_pane("SessCtl")
                self.fresh_marker(pane)
                res, _ = self.deliver(script, pane, "UserPromptSubmit", mode)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(res.stdout, "", "healthy Herdr must suppress a non-terminal event")
                self.assertFalse(self.paths(pane)[1].exists())


class ArgvJsonSessionIdTests(_InvariantGuardCase):
    """Plan §10.1 #61."""

    SID = "codex_argv_sid_0123456789"

    def _argv_json(self, event, key="hook_event_name", sid_first=True, sid=None, indent=None):
        sid = self.SID if sid is None else sid
        pairs = [("session_id", sid), (key, event)]
        if not sid_first:
            pairs.reverse()
        return json.dumps(dict(pairs), indent=indent)

    def test_p61_argv_json_session_id_recorded_on_fall_through(self):
        """Plan §10.1 #61: a non-terminal $1 JSON payload falling through (Herdr unhealthy) records the
        extracted session_id as the .vendor_active UUID record, whatever the key order or line layout."""
        script = self.script("guard-61-record.sh")
        for herdr_state in ("dead", "no-marker"):
            for sid_first, indent in ((True, None), (False, None), (True, 2), (False, 2)):
                with self.subTest(herdr=herdr_state, sid_first=sid_first, indent=indent):
                    if herdr_state == "dead":
                        self.clear_fake_processes()
                        pane = self.new_pane("ArgvDead")
                        self.fresh_marker(pane)
                    else:
                        self.set_herdr_alive()
                        pane = self.new_pane("ArgvNoMarker")
                    _, va = self.paths(pane)
                    payload = self._argv_json("UserPromptSubmit", sid_first=sid_first, indent=indent)
                    res = run_guard(script, payload,
                                    env_extra={"HERDR_PANE_ID": pane})
                    self.assert_passthrough(res, "")
                    self.assertTrue(va.exists(), "fall-through must record .vendor_active")
                    self.assertEqual(json.loads(va.read_text()), {"vendor_session_id": self.SID})

    def test_p61_argv_json_invalid_session_id_stays_bare(self):
        """Plan §10.1 #61: a session_id outside [A-Za-z0-9_-]{16,64} is not extracted; fall-through leaves a bare touch."""
        self.clear_fake_processes()
        script = self.script("guard-61-invalid.sh")
        for bad in ("short_sid", "has space in the session id", "x" * 65):
            with self.subTest(sid=bad):
                pane = self.new_pane("ArgvBad")
                _, va = self.paths(pane)
                res = run_guard(script, self._argv_json("UserPromptSubmit", sid=bad),
                                env_extra={"HERDR_PANE_ID": pane})
                self.assert_passthrough(res, "")
                self.assertTrue(va.exists())
                self.assertEqual(va.read_text(), "", "an invalid session id must leave a bare touch")

    def test_p61_turn_terminal_argv_json_upgrades_bare_touch(self):
        """Plan §10.1 #61: under a bare touch with Herdr healthy, a turn-terminal $1 JSON payload passes
        through and upgrades the bare touch to the extracted UUID record (kept, not unlinked)."""
        script = self.script("guard-61-upgrade.sh")
        for key in ("hook_event_name", "type"):
            with self.subTest(key=key):
                pane = self.new_pane("ArgvUpgrade")
                self.fresh_marker(pane)
                _, va = self.paths(pane)
                va.write_text("")
                res = run_guard(script, self._argv_json("agent-turn-complete", key=key),
                                env_extra={"HERDR_PANE_ID": pane})
                self.assert_passthrough(res, "")
                self.assertTrue(va.exists(), "turn-terminal must keep .vendor_active")
                self.assertEqual(json.loads(va.read_text()), {"vendor_session_id": self.SID})

                # Now UUID-bearing: a healthy non-terminal argv JSON event is suppressed and the record kept.
                res = run_guard(script, self._argv_json("agent-busy", key=key), env_extra={"HERDR_PANE_ID": pane})
                self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)
                self.assertEqual(json.loads(va.read_text()), {"vendor_session_id": self.SID})

                # Session-terminal argv JSON passes through and unlinks the record.
                res = run_guard(script, self._argv_json("session-end", key=key), env_extra={"HERDR_PANE_ID": pane})
                self.assert_passthrough(res, "")
                self.assertFalse(va.exists())


class FailOpenWithoutArgvTests(_InvariantGuardCase):
    """Plan §10.1 #62."""

    def _shells(self):
        shells = [("bash", "#!/bin/bash\nset -u")]
        dash = shutil.which("dash")
        if dash:
            shells.append(("dash", f"#!{dash}\nset -u"))
        return shells

    def _run_on_tty(self, script, pane, *args):
        master, slave = pty.openpty()
        try:
            self.assertTrue(os.isatty(slave))
            return run_guard(script, *args, stdin=slave, env_extra={"HERDR_PANE_ID": pane})
        finally:
            os.close(slave)
            os.close(master)

    def test_p62_tty_stdin_without_argv_fails_open(self):
        """Plan §10.1 #62: stdin is a real TTY, no $1, fresh marker, Herdr alive: the guard passes through."""
        for shell, shebang in self._shells():
            with self.subTest(shell=shell):
                pane = self.new_pane(f"Tty{shell}")
                self.fresh_marker(pane)
                script = self.script(f"guard-62-tty-{shell}.sh", 'echo "PASSTHROUGH_62"', shebang=shebang)
                res = self._run_on_tty(script, pane)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(res.stdout.strip(), "PASSTHROUGH_62", "a TTY with no argv must fail open")

    def test_p62_tty_control_with_argv_token_is_suppressed(self):
        """Plan §10.1 #62 (control): on the same TTY + healthy pane, argv 'Working' is suppressed."""
        for shell, shebang in self._shells():
            with self.subTest(shell=shell):
                pane = self.new_pane(f"TtyCtl{shell}")
                self.fresh_marker(pane)
                script = self.script(f"guard-62-ttyctl-{shell}.sh", 'echo "PASSTHROUGH_62"', shebang=shebang)
                res = self._run_on_tty(script, pane, "Working")
                self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)

    def test_p62_closed_empty_pipe_without_argv_fails_open(self):
        """Plan §10.1 #62: a real closed empty pipe (EOF, not /dev/null) with no $1 passes through and
        leaves no capture file; the same pipe with argv 'Working' is suppressed."""
        for shell, shebang in self._shells():
            for argv, expect in (((), "PASSTHROUGH_62\n"), (("Working",), "")):
                with self.subTest(shell=shell, argv=argv):
                    pane = self.new_pane(f"Pipe{shell}")
                    self.fresh_marker(pane)
                    script = self.script(f"guard-62-pipe-{shell}.sh", 'echo "PASSTHROUGH_62"', shebang=shebang)
                    read_fd, write_fd = os.pipe()
                    os.close(write_fd)
                    try:
                        res = run_guard(script, *argv, stdin=read_fd, env_extra={"HERDR_PANE_ID": pane})
                    finally:
                        os.close(read_fd)
                    self.assertEqual((res.returncode, res.stdout), (0, expect), res.stderr)
                    self.assertEqual(sorted(p.name for p in self.state_dir.rglob(".guard_*")), [])


class TurnTerminalUnderUuidRecordTests(_InvariantGuardCase):
    """Plan §10.1 #67."""

    SEEDED = {"vendor_session_id": "seeded_uuid_record_067"}

    def _seed(self, pane):
        self.fresh_marker(pane)
        _, va = self.paths(pane)
        va.write_text(json.dumps(self.SEEDED, separators=(",", ":")))
        return va

    def test_p67_all_turn_terminal_events_pass_under_uuid_record(self):
        """Plan §10.1 #67: under a UUID .vendor_active, fresh marker and Herdr alive, all 5 turn-terminal
        events in all 3 delivery modes pass through and leave .vendor_active present and byte-identical."""
        script = self.script("guard-67.sh")
        for event in TURN_TERMINAL:
            for mode in DELIVERY_MODES:
                with self.subTest(event=event, mode=mode):
                    pane = self.new_pane("TurnUuid")
                    va = self._seed(pane)
                    before = va.read_bytes()
                    res, sent = self.deliver(script, pane, event, mode)
                    self.assert_passthrough(res, sent)
                    self.assertTrue(va.exists(), "turn-terminal must retain the UUID record")
                    self.assertEqual(va.read_bytes(), before, "turn-terminal must not rewrite the UUID record")

    def test_p67_control_non_terminal_under_uuid_record_is_suppressed(self):
        """Plan §10.1 #67 (control): the same seeded pane suppresses a non-terminal event in every mode
        and keeps the record."""
        script = self.script("guard-67-control.sh")
        for mode in DELIVERY_MODES:
            with self.subTest(mode=mode):
                pane = self.new_pane("TurnCtl")
                va = self._seed(pane)
                before = va.read_bytes()
                res, _ = self.deliver(script, pane, "Working", mode)
                self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)
                self.assertEqual(va.read_bytes(), before)
