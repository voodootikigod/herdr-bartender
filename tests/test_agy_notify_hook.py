"""Unit tests for standalone Antigravity CLI lifecycle hook (scripts/agy-notify-hook.sh)."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from tests.support import REPO_ROOT, SandboxTestCase

HOOK_SCRIPT = REPO_ROOT / "scripts" / "agy-notify-hook.sh"


class AgyNotifyHookTests(SandboxTestCase):
    start_bridge = True

    def _run_hook(
        self,
        event: str | None,
        payload: dict | str | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        env = {
            **os.environ,
            "NOTCHBAR_AGENTS_HOST": "127.0.0.1",
            "NOTCHBAR_AGENTS_PORT": str(self.bridge.port),
        }
        if event is not None:
            env["AGY_HOOK_EVENT"] = event
        else:
            env.pop("AGY_HOOK_EVENT", None)

        if env_extra:
            env.update(env_extra)

        if isinstance(payload, dict):
            stdin_data = json.dumps(payload)
        elif isinstance(payload, str):
            stdin_data = payload
        else:
            stdin_data = "{}"

        proc = subprocess.run(
            ["/bin/bash", str(HOOK_SCRIPT)],
            input=stdin_data,
            text=True,
            capture_output=True,
            env=env,
            timeout=5,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

    def _wait_for_history(self, count: int = 1, timeout: float = 2.0) -> None:
        t0 = time.monotonic()
        while len(self.bridge.history) < count and (time.monotonic() - t0) < timeout:
            time.sleep(0.02)

    def _fresh_marker(self, pane_id: str) -> Path:
        hex_pane = pane_id.encode("utf-8").hex()
        marker = self.state_dir / "panes" / hex_pane
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("active")
        return marker

    def test_herdr_pane_suppression_when_healthy(self):
        """When HERDR_PANE_ID is set AND Herdr is healthy (fresh marker + process alive), the hook suppresses."""
        self._fresh_marker("ws1:p10")

        for event, expected_stdout in (
            ("PreInvocation", "{}"),
            ("PreToolUse", '{"decision":"allow"}'),
            ("PostInvocation", "{}"),
            ("Stop", '{"decision":""}'),
        ):
            with self.subTest(event=event):
                code, out, _ = self._run_hook(
                    event,
                    payload={"conversationId": "c-test-suppress-0001"},
                    env_extra={"HERDR_PANE_ID": "ws1:p10"},
                )
                self.assertEqual(code, 0)
                self.assertEqual(out, expected_stdout)

        # Confirm no events were delivered to the bridge
        self.assertEqual(len(self.bridge.history), 0)

    def test_fail_open_predicates(self):
        """Each unhealthy condition causes the hook to fail open, delivering to the bridge and recording .vendor_active."""
        conditions = [
            ("missing_marker", lambda p, m: m.unlink(missing_ok=True)),
            ("disabled_flag", lambda p, m: (self.state_dir / "DISABLED").write_text("1")),
            ("delivery_down_flag", lambda p, m: (self.state_dir / "DELIVERY_DOWN").write_text("1")),
            ("failed_marker", lambda p, m: (m.parent / f"{m.name}.failed").write_text("1")),
            ("stale_marker", lambda p, m: os.utime(m, (time.time() - 120, time.time() - 120))),
            ("symlink_marker", lambda p, m: (m.unlink(missing_ok=True), m.symlink_to(self.tmp))),
            ("herdr_dead", lambda p, m: (self.clear_fake_processes(), self.add_fake_process("Bartender 6", pid=424200))),
            ("bare_pane_no_ws", lambda p, m: None),
        ]

        for label, setup_fn in conditions:
            with self.subTest(condition=label):
                self.bridge.history.clear()
                self.set_herdr_alive()
                for f in ("DISABLED", "DELIVERY_DOWN"):
                    (self.state_dir / f).unlink(missing_ok=True)

                pane_id = f"ws1:p{label}"
                hex_pane = pane_id.encode("utf-8").hex()
                marker = self._fresh_marker(pane_id)
                va_file = self.state_dir / "panes" / f"{hex_pane}.vendor_active"
                va_file.unlink(missing_ok=True)

                setup_fn(pane_id, marker)

                env = {"HERDR_PANE_ID": pane_id} if label != "bare_pane_no_ws" else {"HERDR_PANE_ID": "pBare"}
                cid = f"conv-failopen-{label[:10]}-0001"
                code, out, _ = self._run_hook(
                    "PreInvocation",
                    payload={"conversationId": cid},
                    env_extra=env,
                )
                self.assertEqual(code, 0)
                self.assertEqual(out, "{}")

                self._wait_for_history(1)
                self.assertEqual(len(self.bridge.history), 1)
                self.assertEqual(self.bridge.history[-1].get("session_id"), cid)

                if label != "bare_pane_no_ws":
                    self.assertTrue(va_file.exists(), f"vendor_active missing for {label}")
                    self.assertIn(cid, va_file.read_text())

    def test_fail_open_recover_stop_handoff(self):
        """When an earlier fail-open wrote .vendor_active, Stop delivers Ended and retires .vendor_active."""
        pane_id = "ws1:pHandoff"
        hex_pane = pane_id.encode("utf-8").hex()
        cid = "conv-handoff-session-0001"
        va_file = self.state_dir / "panes" / f"{hex_pane}.vendor_active"

        # Step 1: PreInvocation fails open (no marker yet)
        code, out, _ = self._run_hook(
            "PreInvocation",
            payload={"conversationId": cid},
            env_extra={"HERDR_PANE_ID": pane_id},
        )
        self.assertEqual(code, 0)
        self._wait_for_history(1)
        self.assertEqual(len(self.bridge.history), 1)
        self.assertEqual(self.bridge.history[-1].get("state"), "Working")
        self.assertTrue(va_file.exists())

        # Step 2: Herdr recovers and creates a fresh marker
        self._fresh_marker(pane_id)

        # Step 3: Stop runs. Because .vendor_active was left, Stop passes through to deliver Ended
        code, out, _ = self._run_hook(
            "Stop",
            payload={"conversationId": cid},
            env_extra={"HERDR_PANE_ID": pane_id},
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, '{"decision":""}')

        self._wait_for_history(2)
        self.assertEqual(len(self.bridge.history), 2)
        self.assertEqual(self.bridge.history[-1].get("state"), "Ended")
        self.assertEqual(self.bridge.history[-1].get("session_id"), cid)
        # .vendor_active should now be retired
        self.assertFalse(va_file.exists())

    def test_fail_open_recover_post_invocation_dismisses_direct_entry(self):
        """When Herdr recovers, the next non-Stop event immediately dismisses the direct entry so no duplicate remains."""
        pane_id = "ws1:pRecovPost"
        hex_pane = pane_id.encode("utf-8").hex()
        cid = "conv-recov-post-000001"
        va_file = self.state_dir / "panes" / f"{hex_pane}.vendor_active"

        # Step 1: PreInvocation fails open (no marker)
        code, out, _ = self._run_hook(
            "PreInvocation",
            payload={"conversationId": cid},
            env_extra={"HERDR_PANE_ID": pane_id},
        )
        self.assertEqual(code, 0)
        self._wait_for_history(1)
        self.assertEqual(len(self.bridge.history), 1)
        self.assertEqual(self.bridge.history[-1].get("state"), "Working")
        self.assertTrue(va_file.exists())

        # Step 2: Herdr recovers and creates fresh marker
        self._fresh_marker(pane_id)

        # Step 3: PostInvocation arrives while Herdr is healthy.
        # It suppresses the standalone PostInvocation, BUT immediately ends the previous direct session!
        code, out, _ = self._run_hook(
            "PostInvocation",
            payload={"conversationId": cid},
            env_extra={"HERDR_PANE_ID": pane_id},
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "{}")

        self._wait_for_history(2)
        self.assertEqual(len(self.bridge.history), 2)
        self.assertEqual(self.bridge.history[-1].get("state"), "Ended")
        self.assertEqual(self.bridge.history[-1].get("session_id"), cid)
        self.assertFalse(va_file.exists(), ".vendor_active must be unlinked upon handoff dismissal")

    def test_subshell_wrapper_preserves_stable_session_id(self):
        """Spawning via the README sh -c wrapper produces stable session IDs across invocations without conversationId."""
        self.bridge.history.clear()
        cmd = f'if [ -x "{HOOK_SCRIPT}" ]; then AGY_HOOK_EVENT="$1" "{HOOK_SCRIPT}"; fi'
        env = {
            **os.environ,
            "NOTCHBAR_AGENTS_HOST": "127.0.0.1",
            "NOTCHBAR_AGENTS_PORT": str(self.bridge.port),
            "TERM_SESSION_ID": "term-sess-fixed-1234",
        }
        payload = json.dumps({"workspacePaths": ["/Users/tester/subshell_project"]})

        # Run 1: PreInvocation via sh -c wrapper
        p1 = subprocess.run(["sh", "-c", cmd, "sh", "PreInvocation"], input=payload, text=True, capture_output=True, env=env)
        self.assertEqual(p1.returncode, 0)
        self._wait_for_history(1)
        sid1 = self.bridge.history[-1].get("session_id")
        pid1 = self.bridge.history[-1].get("pid")

        # Run 2: PostInvocation via sh -c wrapper
        p2 = subprocess.run(["sh", "-c", cmd, "sh", "PostInvocation"], input=payload, text=True, capture_output=True, env=env)
        self.assertEqual(p2.returncode, 0)
        self._wait_for_history(2)
        sid2 = self.bridge.history[-1].get("session_id")
        pid2 = self.bridge.history[-1].get("pid")

        # Must not match the transient sh PID and must stay strictly identical across calls
        self.assertEqual(sid1, sid2)
        self.assertEqual(pid1, pid2)
        self.assertTrue(sid1.startswith("agy-session-"))

    def test_event_resolved_from_json_body_alone(self):
        """When AGY_HOOK_EVENT and argv are unset, EVENT is correctly resolved from hook_event_name in JSON."""
        # 1. PreToolUse in JSON
        self.bridge.history.clear()
        code, out, _ = self._run_hook(
            None,
            payload={
                "hook_event_name": "PreToolUse",
                "conversationId": "conv-json-event-001",
                "toolCall": {"name": "run_command"},
            },
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, '{"decision":"allow"}')
        self._wait_for_history(1)
        self.assertEqual(self.bridge.history[-1].get("state"), "Working")
        self.assertEqual(self.bridge.history[-1].get("title"), "Tool: run_command")

        # 2. Stop in JSON
        code, out, _ = self._run_hook(
            None,
            payload={
                "hook_event_name": "Stop",
                "conversationId": "conv-json-event-001",
            },
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, '{"decision":""}')
        self._wait_for_history(2)
        self.assertEqual(self.bridge.history[-1].get("state"), "Ended")

    def test_string_sanitization_and_length_caps(self):
        """OSC, ANSI CSI, bidi overrides, and invalid controls are stripped; lengths are capped."""
        dirty_tool = "\x1b]0;pwn\x07\u202eexe.txt\u202c\x1b[31mrun_command\x1b[0m"
        long_cwd = "/dir/" + "x" * 500

        code, out, _ = self._run_hook(
            "PreToolUse",
            payload={
                "conversationId": "valid-conv-uuid-0001",
                "toolCall": {"name": dirty_tool},
                "workspacePaths": [long_cwd],
            },
        )
        self.assertEqual(code, 0)
        self._wait_for_history(1)
        ev = self.bridge.history[-1]
        self.assertEqual(ev.get("title"), "Tool: exe.txtrun_command")
        self.assertEqual(len(ev.get("cwd")), 256)
        self.assertEqual(ev.get("session_id"), "valid-conv-uuid-0001")

    def test_invalid_conversation_id_falls_back_to_safe_deterministic_id(self):
        """Non-string or malformed conversationId safely falls back to a deterministic 36-char session ID."""
        for bad_id in (None, 12345, "a" * 100, "has spaces", {"obj": 1}):
            with self.subTest(bad_id=bad_id):
                self.bridge.history.clear()
                code, out, _ = self._run_hook(
                    "PreInvocation",
                    payload={"conversationId": bad_id, "workspacePaths": ["/Users/tester/proj1"]},
                )
                self.assertEqual(code, 0)
                self._wait_for_history(1)
                sid = self.bridge.history[-1].get("session_id")
                self.assertTrue(sid.startswith("agy-session-"), f"unexpected sid: {sid}")
                self.assertEqual(len(sid), 36)

    def test_stable_fallback_session_id_across_calls(self):
        """Two hook calls without conversationId in the same workspace produce the identical session ID."""
        self.bridge.history.clear()
        payload = {"workspacePaths": ["/Users/tester/same_project"]}

        # Call 1: PreInvocation
        code1, _, _ = self._run_hook("PreInvocation", payload=payload)
        self.assertEqual(code1, 0)
        self._wait_for_history(1)
        sid1 = self.bridge.history[-1].get("session_id")

        # Call 2: PostInvocation
        code2, _, _ = self._run_hook("PostInvocation", payload=payload)
        self.assertEqual(code2, 0)
        self._wait_for_history(2)
        sid2 = self.bridge.history[-1].get("session_id")

        self.assertEqual(sid1, sid2)
        self.assertTrue(sid1.startswith("agy-session-"))

    def test_python_unavailable_fallback_and_event_whitelist(self):
        """When Python is unavailable, bash fallback correctly maps Stop to Ended, PostInvocation to Idle, and whitelists EVENT."""
        no_py_dir = self.tmp / "no-py-bin"
        no_py_dir.mkdir(parents=True, exist_ok=True)
        fake_py = no_py_dir / "python3"
        fake_py.write_text("#!/bin/sh\nexit 127\n")
        fake_py.chmod(0o755)

        env = {"PATH": f"{no_py_dir}:{os.environ.get('PATH', '')}"}

        # 1. PreInvocation -> Working
        self.bridge.history.clear()
        code, out, _ = self._run_hook("PreInvocation", payload={}, env_extra=env)
        self.assertEqual(code, 0)
        self._wait_for_history(1)
        self.assertEqual(self.bridge.history[-1].get("state"), "Working")

        # 2. PostInvocation -> Idle
        code, out, _ = self._run_hook("PostInvocation", payload={}, env_extra=env)
        self.assertEqual(code, 0)
        self._wait_for_history(2)
        self.assertEqual(self.bridge.history[-1].get("state"), "Idle")

        # 3. Stop -> Ended
        code, out, _ = self._run_hook("Stop", payload={}, env_extra=env)
        self.assertEqual(code, 0)
        self.assertEqual(out, '{"decision":""}')
        self._wait_for_history(3)
        self.assertEqual(self.bridge.history[-1].get("state"), "Ended")

        # 4. Injected event -> whitelisted to empty, safe fallback
        code, out, _ = self._run_hook('Stop"; injection', payload={}, env_extra=env)
        self.assertEqual(code, 0)
        self._wait_for_history(4)
        self.assertEqual(self.bridge.history[-1].get("event"), "")

    def test_bartender_not_running_skips_delivery(self):
        """When Bartender process is not running, hook skips network request and exits 0 cleanly."""
        self.clear_fake_processes()  # no Bartender process registered
        self.set_herdr_alive()
        self.bridge.history.clear()
        code, out, _ = self._run_hook(
            "PreInvocation",
            payload={"conversationId": "conv-nobartender-01"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "{}")
        # Nothing sent because Bartender was not running
        self.assertEqual(len(self.bridge.history), 0)

    def test_terminal_name_mapping(self):
        """TERM_PROGRAM values are mapped to human-readable names for Bartender."""
        cases = [
            ("ghostty", "Ghostty"),
            ("vscode", "VS Code"),
            ("Apple_Terminal", "Terminal"),
            ("iTerm.app", "iTerm"),
            ("CustomTerm", "CustomTerm"),
        ]
        for term_prog, expected_name in cases:
            with self.subTest(term_prog=term_prog):
                self.bridge.history.clear()
                code, out, _ = self._run_hook(
                    "PreInvocation",
                    payload={"conversationId": "conv-term-test-01"},
                    env_extra={"TERM_PROGRAM": term_prog},
                )
                self.assertEqual(code, 0)
                self._wait_for_history(1)
                self.assertEqual(self.bridge.history[-1].get("terminal"), expected_name)

    def test_loopback_only_ignores_notchbar_agents_host(self):
        """Hook strictly connects to 127.0.0.1, ignoring NOTCHBAR_AGENTS_HOST."""
        self.bridge.history.clear()
        code, out, _ = self._run_hook(
            "PreInvocation",
            payload={"conversationId": "conv-loopback-001"},
            env_extra={"NOTCHBAR_AGENTS_HOST": "192.0.2.1"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "{}")
        self._wait_for_history(1)
        self.assertEqual(len(self.bridge.history), 1)
        self.assertEqual(self.bridge.history[-1].get("session_id"), "conv-loopback-001")
