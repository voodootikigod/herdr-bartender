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
        event: str,
        payload: dict | str | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        env = {
            **os.environ,
            "AGY_HOOK_EVENT": event,
            "NOTCHBAR_AGENTS_HOST": "127.0.0.1",
            "NOTCHBAR_AGENTS_PORT": str(self.bridge.port),
        }
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

    def test_herdr_pane_suppression_when_healthy(self):
        """When HERDR_PANE_ID is set AND Herdr is healthy (fresh marker + process alive), the hook suppresses."""
        # Create fresh pane marker for ws1:p10
        hex_pane = "ws1:p10".encode("utf-8").hex()
        marker = self.state_dir / "panes" / hex_pane
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("active")

        for event, expected_stdout in (
            ("PreInvocation", "{}"),
            ("PreToolUse", '{"decision":"allow"}'),
            ("PostInvocation", "{}"),
            ("Stop", '{"decision":""}'),
        ):
            with self.subTest(event=event):
                code, out, _ = self._run_hook(
                    event,
                    payload={"conversationId": "c-test-suppress"},
                    env_extra={"HERDR_PANE_ID": "ws1:p10"},
                )
                self.assertEqual(code, 0)
                self.assertEqual(out, expected_stdout)

        # Confirm no events were delivered to the bridge
        self.assertEqual(len(self.bridge.history), 0)

    def test_herdr_pane_fails_open_when_unhealthy(self):
        """When HERDR_PANE_ID is set but Herdr is dead or marker missing, suppression fails open and delivers."""
        # No pane marker created -> Herdr not actively managing pane
        code, out, _ = self._run_hook(
            "PreInvocation",
            payload={"conversationId": "c-unhealthy"},
            env_extra={"HERDR_PANE_ID": "ws1:pUnhealthy"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "{}")

        self._wait_for_history(1)
        self.assertEqual(len(self.bridge.history), 1)
        self.assertEqual(self.bridge.history[-1].get("session_id"), "agy:c-unhealthy")

    def test_standalone_pre_invocation(self):
        """Standalone PreInvocation emits Working with Thinking... title to Bartender."""
        code, out, _ = self._run_hook(
            "PreInvocation",
            payload={
                "conversationId": "c-inv-1",
                "workspacePaths": ["/Users/tester/myproject"],
            },
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "{}")

        self._wait_for_history(1)
        self.assertEqual(len(self.bridge.history), 1)
        ev = self.bridge.history[-1]
        self.assertEqual(ev.get("agent"), "Antigravity")
        self.assertEqual(ev.get("state"), "Working")
        self.assertEqual(ev.get("title"), "Thinking...")
        self.assertEqual(ev.get("session_id"), "agy:c-inv-1")
        self.assertEqual(ev.get("cwd"), "/Users/tester/myproject")

    def test_standalone_pre_tool_use(self):
        """Standalone PreToolUse emits Working with tool name and returns decision: allow."""
        code, out, _ = self._run_hook(
            "PreToolUse",
            payload={
                "conversationId": "c-tool-1",
                "toolCall": {"name": "run_command", "args": {"CommandLine": "cargo test"}},
            },
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, '{"decision":"allow"}')

        self._wait_for_history(1)
        self.assertEqual(len(self.bridge.history), 1)
        ev = self.bridge.history[-1]
        self.assertEqual(ev.get("agent"), "Antigravity")
        self.assertEqual(ev.get("state"), "Working")
        self.assertEqual(ev.get("title"), "Tool: run_command")
        self.assertEqual(ev.get("session_id"), "agy:c-tool-1")

    def test_standalone_post_invocation(self):
        """Standalone PostInvocation emits Idle to Bartender."""
        code, out, _ = self._run_hook(
            "PostInvocation",
            payload={"conversationId": "c-idle-1"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "{}")

        self._wait_for_history(1)
        self.assertEqual(len(self.bridge.history), 1)
        ev = self.bridge.history[-1]
        self.assertEqual(ev.get("agent"), "Antigravity")
        self.assertEqual(ev.get("state"), "Idle")
        self.assertEqual(ev.get("session_id"), "agy:c-idle-1")

    def test_standalone_stop(self):
        """Standalone Stop emits Ended synchronously to dismiss the item."""
        code, out, _ = self._run_hook(
            "Stop",
            payload={"conversationId": "c-stop-1"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, '{"decision":""}')

        self._wait_for_history(1)
        self.assertEqual(len(self.bridge.history), 1)
        ev = self.bridge.history[-1]
        self.assertEqual(ev.get("agent"), "Antigravity")
        self.assertEqual(ev.get("state"), "Ended")
        self.assertEqual(ev.get("session_id"), "agy:c-stop-1")

    def test_missing_conversation_id_falls_back_to_pid(self):
        """When conversationId is absent, session_id falls back to a per-PID ID, not a shared constant."""
        code, out, _ = self._run_hook(
            "PreInvocation",
            payload={"workspacePaths": ["/Users/tester/p"]},
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "{}")

        self._wait_for_history(1)
        self.assertEqual(len(self.bridge.history), 1)
        sid = self.bridge.history[-1].get("session_id")
        self.assertTrue(sid.startswith("agy:pid:"), f"unexpected session_id: {sid}")

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
                    payload={"conversationId": "c-term"},
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
            payload={"conversationId": "c-loopback"},
            env_extra={"NOTCHBAR_AGENTS_HOST": "192.0.2.1"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "{}")
        # Should still reach our local mock bridge on 127.0.0.1
        self._wait_for_history(1)
        self.assertEqual(len(self.bridge.history), 1)
        self.assertEqual(self.bridge.history[-1].get("session_id"), "agy:c-loopback")

    def test_fail_safe_on_malformed_input(self):
        """Hook handles unparseable JSON or empty input cleanly with 0 exit code."""
        code, out, _ = self._run_hook("PreToolUse", payload="{malformed json")
        self.assertEqual(code, 0)
        self.assertEqual(out, '{"decision":"allow"}')

        code, out, _ = self._run_hook("PreInvocation", payload="")
        self.assertEqual(code, 0)
        self.assertEqual(out, "{}")
