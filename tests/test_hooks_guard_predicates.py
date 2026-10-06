"""Vendor-hook guard: top-level event classification (R36), the python3 capture fallback, and the
fail-open predicates of the healthy-suppression check (DISABLED, DELIVERY_DOWN, .failed, the 60s marker age).

Every case runs the REAL guard (hook_guard.sh) inside the sandbox; Herdr liveness comes from the pgrep shim.
"""

import json
import os
import shutil
import time
import unittest

from herdr_bartender.markers import refresh_pane_marker
from tests.support import run_guard
from tests.support.guard_harness import leftovers, make_shim, path_with, start_fifo_writer
from tests.test_hooks_guard import _GuardCase

UUID_RECORD = '{"vendor_session_id":"vendor_uuid_record_0001"}'
SID = "top_level_session_0001"
PASS = "VENDOR_RAN"


def payload(event, indent=None, **extra):
    return json.dumps({"session_id": SID, "hook_event_name": event, **extra}, indent=indent)


class TopLevelClassificationTests(_GuardCase):
    """R36 (Plan §1 L40 "top-level"): only the payload's own top-level event / session_id members count."""

    def setUp(self):
        super().setUp()
        self.vendor = self.script("guard-classify.sh", f'echo "{PASS}"')

    def owned_pane(self, name, record=UUID_RECORD):
        """Fresh marker, Herdr alive and a UUID .vendor_active: a non-terminal event is suppressed."""
        pane = f"w1:p{name}"
        self.fresh_marker(pane)
        _, va = self.paths(pane)
        va.write_text(record)
        return pane, va

    def test_nested_terminal_values_never_classify_the_event(self):
        """Plan §10.1 #33 / R36. Findings (security/bash): a nested ``"state":"Ended"`` / ``"event":"SessionEnd"`` in tool_input made a
        PreToolUse session-terminal (pass-through + .vendor_active deleted); a nested ``"state":"Done"`` /
        ``"type":"Stop"`` made it turn-terminal (a duplicate vendor notification). Both are suppressed now."""
        cases = {
            "state-ended": {"tool_input": {"id": "1", "state": "Ended"}},
            "event-sessionend": {"tool_input": {"event": "SessionEnd"}},
            "state-done": {"tool_input": {"id": "ENG-1", "state": "Done"}},
            "type-stop": {"tool_response": {"type": "Stop"}},
            "deep-array": {"tool_input": {"items": [{"hook_event_name": "SessionEnd"}]}},
        }
        for label, extra in cases.items():
            for indent in (None, 2):
                with self.subTest(case=label, indent=indent):
                    pane, va = self.owned_pane(f"Nested{label.replace('-', '')}{indent or 0}")
                    res = run_guard(self.vendor, input=payload("PreToolUse", indent=indent, **extra),
                                    env_extra={"HERDR_PANE_ID": pane})
                    self.assertEqual(res.returncode, 0, res.stderr)
                    self.assertNotIn(PASS, res.stdout, "a nested key must not classify the vendor event")
                    self.assertEqual(va.read_text(), UUID_RECORD, ".vendor_active must be kept")

    def test_nested_terminal_value_in_argv_json(self):
        """Plan §10.1 #61 / R36: the same rule for the Codex argv ``$1`` JSON payload."""
        pane, va = self.owned_pane("NestedArgv")
        argv = json.dumps({"type": "agent-busy", "thread-id": "t", "payload": {"type": "Ended"}})
        res = run_guard(self.vendor, argv, env_extra={"HERDR_PANE_ID": pane})
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)
        self.assertEqual(va.read_text(), UUID_RECORD)

    def test_top_level_terminal_after_a_nested_object_still_counts(self):
        pane, va = self.owned_pane("TopAfterNested")
        body = json.dumps({"session_id": SID, "tool_input": {"state": "Working"}, "hook_event_name": "SessionEnd"})
        res = run_guard(self.vendor, input=body, env_extra={"HERDR_PANE_ID": pane})
        self.assertIn(PASS, res.stdout)
        self.assertFalse(va.exists(), "a top-level SessionEnd unlinks .vendor_active")

    def test_pretty_printed_stdin_is_classified(self):
        """Plan §10.1 #33 / R36. Low finding: multi-line JSON on stdin was never classified terminal (SessionEnd suppressed, .vendor_active
        kept; Stop under a UUID record suppressed)."""
        pane, va = self.owned_pane("PrettyEnd")
        res = run_guard(self.vendor, input=payload("SessionEnd", indent=2) + "\n", env_extra={"HERDR_PANE_ID": pane})
        self.assertIn(PASS, res.stdout)
        self.assertFalse(va.exists())
        pane, va = self.owned_pane("PrettyStop")
        res = run_guard(self.vendor, input=payload("Stop", indent=4), env_extra={"HERDR_PANE_ID": pane})
        self.assertIn(PASS, res.stdout, "turn-terminal under a UUID record passes through")
        self.assertEqual(va.read_text(), UUID_RECORD)

    def test_escaped_quotes_and_strings_never_open_a_member(self):
        """Keys inside strings (escaped JSON, braces in text) are content, never members."""
        pane, va = self.owned_pane("Escaped")
        extra = {"prompt": 'say {"state":"Ended"} and "type":"Stop" \\ }', "transcript": "]}{["}
        res = run_guard(self.vendor, input=payload("UserPromptSubmit", **extra), env_extra={"HERDR_PANE_ID": pane})
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)
        self.assertEqual(va.read_text(), UUID_RECORD)

    def test_only_a_top_level_session_id_is_recorded(self):
        """A session_id nested in tool_input is not the vendor session: fall-through leaves a bare touch."""
        self.set_herdr_dead()
        pane = "w1:pNestedSid"
        _, va = self.paths(pane)
        body = json.dumps({"hook_event_name": "PreToolUse", "tool_input": {"session_id": "nested_session_id_0001"}})
        res = run_guard(self.vendor, input=body, env_extra={"HERDR_PANE_ID": pane})
        self.assertIn(PASS, res.stdout)
        self.assertEqual(va.read_text(), "", "no top-level session_id: bare touch")
        pane = "w1:pTopSid"
        _, va = self.paths(pane)
        res = run_guard(self.vendor, input=payload("PreToolUse", indent=2, tool_input={"session_id": "x" * 20}),
                        env_extra={"HERDR_PANE_ID": pane})
        self.assertIn(PASS, res.stdout)
        self.assertEqual(json.loads(va.read_text()), {"vendor_session_id": SID})

    def test_unclassifiable_payload_fails_open(self):
        """awk missing or failing: the event cannot be classified, so it is never suppressed."""
        pane, va = self.owned_pane("NoAwk")
        make_shim(self.shim_bin, "awk", "exit 2")
        res = run_guard(self.vendor, input=payload("PreToolUse"),
                        env_extra={"HERDR_PANE_ID": pane, "PATH": path_with(self.shim_bin)})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(PASS, res.stdout)
        self.assertTrue(va.exists())


GUARD_TOOLS = ("bash", "cat", "grep", "od", "tr", "mkdir", "mktemp", "rm", "mkfifo", "awk", "stat", "date",
               "mv", "touch", "sleep", "env", "python3", "head", "wc")


class PythonCaptureFallbackTests(_GuardCase):
    """Finding (bash): with no perl on PATH the python3 capture lost every byte it had read when the 1s deadline hit."""

    def no_perl_path(self):
        bin_dir = self.tmp / "no-perl-bin"
        bin_dir.mkdir(exist_ok=True)
        for tool in GUARD_TOOLS:
            target = shutil.which(tool)
            self.assertIsNotNone(target, tool)
            link = bin_dir / tool
            if not link.exists():
                link.symlink_to(target)
        shims = os.path.join(os.path.dirname(os.path.abspath(__file__)), "support", "shims")
        path = os.pathsep.join([shims, str(bin_dir)])
        self.assertIsNone(shutil.which("perl", path=path), "the fallback branch needs a PATH without perl")
        return path

    def test_stalled_pipe_keeps_the_prefix_read_before_the_deadline(self):
        """Plan §10.1 #56 with perl absent: the prefix read before the 1s deadline and the late remainder both reach
        the vendor byte-exact, also when the writer keeps the pipe open after a complete payload."""
        line1 = '{"session_id":"abcdefabcdefabcdef12",'
        line2 = '"x":1}\n'
        script = self.script("guard-py-capture.sh", "cat")
        for label, writer in (
            ("stalled-mid-payload", f"printf '%s' '{line1}' >&3; sleep 1.5; printf '%s' '{line2}' >&3"),
            ("open-after-payload", f"printf '%s' '{line1}{line2}' >&3; sleep 1.5"),
        ):
            with self.subTest(case=label):
                fifo = self.tmp / f"py_fifo_{label}"
                os.mkfifo(str(fifo))
                start_fifo_writer(self, fifo, f"exec 3>'{fifo}'; {writer}; exec 3>&-")
                with open(str(fifo), "rb") as reader:
                    started = time.monotonic()
                    res = run_guard(script, stdin=reader,
                                    env_extra={"HERDR_PANE_ID": f"w1:pPy{label[:4]}", "PATH": self.no_perl_path()})
                    elapsed = time.monotonic() - started
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(res.stdout, line1 + line2, "the vendor must receive the payload byte-exact")
                self.assertLess(elapsed, 2.5)
                self.assertEqual(leftovers(self.state_dir, ".guard_stdin.*", ".guard_splice.*"), [])

    def test_complete_payload_is_captured_and_classified(self):
        script = self.script("guard-py-full.sh", 'printf "V:"; cat')
        pane = "w1:pPyFull"
        _, va = self.paths(pane)
        va.write_text(UUID_RECORD)
        self.fresh_marker(pane)
        body = payload("SessionEnd", indent=2)
        res = run_guard(script, input=body, env_extra={"HERDR_PANE_ID": pane, "PATH": self.no_perl_path()})
        self.assertEqual((res.returncode, res.stdout), (0, "V:" + body), res.stderr)
        self.assertFalse(va.exists())


class CaptureWriteFailureTests(_GuardCase):
    """Low finding (bash): when writing the stdin capture fails (disk full), the bytes the capture had already read
    from the pipe but could not write were dropped, so the vendor hook got truncated or empty stdin. The capture now
    hands them, with the file's prefix and the rest of stdin, to a splicer feeding a FIFO (perl and python3 alike).
    ``ulimit -f`` with SIGXFSZ ignored makes the capture's write fail with EFBIG after N KiB (0: the first write)."""

    PAYLOAD = bytes(range(256)) * 800 + b"tail\n"   # 200 KiB: several 64 KiB chunks
    no_perl_path = PythonCaptureFallbackTests.no_perl_path

    def run_with_file_limit(self, script, kib, env_extra):
        wrapper = script.with_name(f"{script.name}.fsize{kib}.sh")
        wrapper.write_text(f'#!/usr/bin/env bash\ntrap "" XFSZ\nulimit -f {kib}\nexec "{script}" "$@"\n')
        os.chmod(wrapper, 0o755)
        return run_guard(wrapper, input=self.PAYLOAD, env_extra=env_extra, text=False)

    def test_vendor_gets_every_byte_when_the_capture_cannot_be_written(self):
        script = self.script("guard-capture-full.sh", "cat")
        for capture in ("perl", "python3"):
            for kib in (1, 0):
                with self.subTest(capture=capture, written_kib=kib):
                    pane = f"w1:pFull{capture}{kib}"
                    env = {"HERDR_PANE_ID": pane}
                    if capture == "python3":
                        env["PATH"] = self.no_perl_path()
                    res = self.run_with_file_limit(script, kib, env)
                    self.assertEqual(res.returncode, 0, res.stderr)
                    self.assertEqual(len(res.stdout), len(self.PAYLOAD))
                    self.assertEqual(res.stdout, self.PAYLOAD, "the vendor must receive stdin byte-exact")
                    self.assertEqual(leftovers(self.state_dir, ".guard_stdin.*", ".guard_splice.*"), [])


class SuppressionPredicateTests(_GuardCase):
    """Findings (tests): the guard's fail-open checks had no direct test - deleting the DELIVERY_DOWN or DISABLED
    clause, ignoring ``<hex>.failed`` or widening the 60s marker window all survived the suite."""

    NOW = 1_800_000_000

    def setUp(self):
        super().setUp()
        self.vendor = self.script("guard-predicates.sh", f'echo "{PASS}"')

    def clocked_env(self, pane, stat_flavor=None):
        """``date +%s`` pinned to NOW (no second boundary can flake an age test); optionally a BSD-only stat."""
        bin_dir = self.tmp / f"clock-{stat_flavor or 'native'}"
        make_shim(bin_dir, "date", f'echo "{self.NOW}"')
        if stat_flavor == "bsd":
            mtime = 'exec python3 -c "import os,sys; print(int(os.stat(sys.argv[1]).st_mtime))" "$3"'
            make_shim(bin_dir, "stat", f'if [ "$1" = "-f" ] && [ "$2" = "%m" ]; then {mtime}; fi\n'
                                       'echo "stat: illegal option -- ${1#-}" >&2; exit 1')
        return {"HERDR_PANE_ID": pane, "PATH": path_with(bin_dir)}

    def marker_aged(self, pane, age):
        marker = self.fresh_marker(pane)
        os.utime(marker, (self.NOW - age, self.NOW - age))
        return marker

    def outcome(self, pane, env=None, *args):
        res = run_guard(self.vendor, *(args or ("Working",)), env_extra=env or {"HERDR_PANE_ID": pane})
        self.assertEqual(res.returncode, 0, res.stderr)
        return PASS in res.stdout, self.paths(pane)[1].exists()

    def test_flags_and_failed_force_pass_through_on_a_fresh_marker(self):
        """Plan §10.1 #8 / §1 items 5-6: DISABLED, DELIVERY_DOWN or the pane's .failed disqualify a fresh marker; the vendor
        hook runs and records .vendor_active. The control (no flag) is suppressed."""
        for flag in (None, "DISABLED", "DELIVERY_DOWN", ".failed"):
            with self.subTest(flag=flag):
                pane = f"w1:pFlag{(flag or 'none').strip('.')}"
                self.fresh_marker(pane)
                path = None
                if flag == ".failed":
                    path = self.panes / f"{self.paths(pane)[0].name}.failed"
                elif flag:
                    path = self.state_dir / flag
                if path is not None:
                    path.write_text("1")
                    self.addCleanup(path.unlink, missing_ok=True)
                for event in ("Working", "Stop"):
                    passed, recorded = self.outcome(pane, None, event)
                    self.assertEqual(passed, flag is not None, f"{event}: flag {flag}")
                    self.assertEqual(recorded, flag is not None)
                if path is not None:
                    path.unlink()

    def test_marker_age_window_is_60_seconds(self):
        """Plan §10.1 #23 / §1 item 5 / §5.1 L60: fresh means 0 <= age < 60s. 59s suppresses; 60s and 61s fall through
        (with a pinned clock, under the native stat and a BSD-only stat)."""
        for flavor in (None, "bsd"):
            for age, suppressed in ((0, True), (59, True), (60, False), (61, False), (600, False)):
                with self.subTest(stat=flavor or "native", age=age):
                    pane = f"w1:pAge{flavor or 'n'}{age}"
                    self.marker_aged(pane, age)
                    passed, recorded = self.outcome(pane, self.clocked_env(pane, flavor))
                    self.assertEqual(passed, not suppressed)
                    self.assertEqual(recorded, not suppressed)


class HeartbeatGateTests(_GuardCase):
    """The reconciler heartbeat must not keep a marker fresh for a pane the guard should hand back to the vendor."""

    def aged_marker(self, pane, age=120):
        marker = self.fresh_marker(pane)
        stamp = time.time() - age
        os.utime(marker, (stamp, stamp))
        return marker, stamp

    def test_refresh_is_a_no_op_under_disabled_or_failed(self):
        """Plan §10.1 #8/#57: the heartbeat never refreshes a marker under DISABLED or the pane's .failed."""
        for gate in ("DISABLED", ".failed"):
            with self.subTest(gate=gate):
                pane = f"w1:pBeat{gate.strip('.')}"
                marker, stamp = self.aged_marker(pane)
                flag = self.state_dir / "DISABLED" if gate == "DISABLED" else self.panes / f"{marker.name}.failed"
                flag.write_text("1")
                self.assertFalse(refresh_pane_marker(pane))
                self.assertAlmostEqual(marker.stat().st_mtime, stamp, delta=1)
                flag.unlink()

    def test_refresh_updates_an_existing_marker_otherwise(self):
        marker, stamp = self.aged_marker("w1:pBeatOk")
        self.assertTrue(refresh_pane_marker("w1:pBeatOk"))
        self.assertGreater(marker.stat().st_mtime, stamp + 60)


class UuidRecordMaintenanceTests(_GuardCase):
    """Round-3 finding (tests): with Herdr unhealthy, a non-terminal pass-through rewrites a UUID ``.vendor_active``
    with the CURRENT vendor session (Plan §7.1 L827: "maintains .vendor_active with session ID atomically via
    .va.tmp"). Replacing that rewrite block with ``:`` survived the suite. Here Herdr is alive but the pane has no
    marker (not yet admitted, or handed back to the vendor), so the guard passes through."""

    start_bridge = True
    OLD = "old_vendor_session_aaaa"
    NEW = "new_vendor_session_bbbb"

    def setUp(self):
        super().setUp()
        self.vendor = self.script("guard-uuid-maint.sh", f'cat >/dev/null; echo "{PASS}"')

    def seed_record(self, pane, uuid):
        _, va = self.paths(pane)
        va.write_text('{"vendor_session_id":"%s"}' % uuid)
        aged = time.time() - 600
        os.utime(va, (aged, aged))
        return va, aged

    def pass_through(self, pane, uuid, mode="argv-json"):
        body = json.dumps({"session_id": uuid, "hook_event_name": "UserPromptSubmit"})
        args, kwargs = ((body,), {}) if mode == "argv-json" else ((), {"input": body})
        res = run_guard(self.vendor, *args, env_extra={"HERDR_PANE_ID": pane}, **kwargs)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(PASS, res.stdout, "Herdr unhealthy for this pane: the vendor hook runs")

    def test_unhealthy_pass_through_records_the_current_vendor_session(self):
        """A vendor session that died without SessionEnd is followed by a new one in the same pane: the record
        names the new session (and is refreshed), atomically, with no .va.tmp leftovers."""
        for mode in ("argv-json", "stdin"):
            with self.subTest(mode=mode):
                pane = f"w1:pMaint{mode.replace('-', '')}"
                va, aged = self.seed_record(pane, self.OLD)
                self.pass_through(pane, self.NEW, mode)
                self.assertEqual(json.loads(va.read_text()), {"vendor_session_id": self.NEW})
                self.assertGreater(va.stat().st_mtime, aged + 60)
                self.assertEqual(leftovers(self.state_dir, ".va.tmp.*"), [])

    def test_pane_close_dismisses_the_session_the_guard_recorded_last(self):
        """The later dismissal targets the vendor session that is actually on Top Shelf (the new one), never the
        stale UUID, so the vendor's duplicate entry is removed."""
        from herdr_bartender.handlers import handle_pane_closed
        pane = "w1:pMaintClose"
        va, _ = self.seed_record(pane, self.OLD)
        self.pass_through(pane, self.NEW)
        handle_pane_closed({"pane_id": pane}, {}, bridge_url=self.mock_url)
        dismissed = [body["session_id"] for body in self.bridge.posts()
                     if isinstance(body, dict) and body.get("state") == "Ended"]
        self.assertEqual(dismissed, [self.NEW])
        self.assertFalse(va.exists())


if __name__ == "__main__":
    unittest.main()
