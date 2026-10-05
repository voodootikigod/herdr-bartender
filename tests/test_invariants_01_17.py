"""Plan §10.1 invariants #1-#17: tests that close the gaps left by the coverage audit.

Only #14 was weak: the CLI's "immediately exit 0" DISABLED guard was masked by the cache's own
re-check under the lock, so removing the guard went unnoticed. These tests run the real launcher
as a subprocess (sandboxed, mock bridge, PATH shims) and assert the event process does nothing
observable at all while DISABLED exists: no state files, no bridge traffic, no ps/pgrep calls,
no hand-off spawn.
"""

import json
import os
import stat
import time
import unittest
from pathlib import Path

from tests.support import SandboxTestCase
from tests.support.sandbox import SHIM_DIR

STATUS_ENVELOPE = {
    "event": "pane.agent_status_changed",
    "data": {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1", "agent": "claude",
             "agent_status": "blocked", "title": "Reviewing pull request #42", "timestamp": 1727998410.12},
    "context": {"focused_pane_id": "w1:p1", "focused_pane_agent": "claude", "focused_pane_cwd": "/workspace",
                "workspace_id": "w1", "workspace_label": "Dev", "tab_id": "w1:t1"},
}
CLOSE_ENVELOPE = {"event": "pane.closed", "data": {"pane_id": "w1:p1", "workspace_id": "w1"}}
# Generous for a cold python3 start on a loaded CI box, far below the 1.5s watchdog budget the
# enabled path would arm; the real signal is the absence of side effects asserted alongside it.
DISABLED_EXIT_BUDGET_SECONDS = 1.5
LOGGED_TOOLS = ("ps", "pgrep", "osascript", "herdr")


def _tree(root: Path) -> list:
    """Every path under root, relative and sorted ([] when root does not exist)."""
    if not root.exists():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


class DisabledFlagEventPathTests(SandboxTestCase):
    """Plan §10.1 #14 (event path): DISABLED makes an event invocation a true no-op."""

    start_bridge = True

    def setUp(self) -> None:
        super().setUp()
        self.tool_log = self.sandbox / "tool-calls.log"
        self.log_shims = self.sandbox / "log-shims"
        self.log_shims.mkdir()
        for name in LOGGED_TOOLS:
            shim = self.log_shims / name
            shim.write_text(
                "#!/usr/bin/env bash\n"
                f'echo "{name} $*" >> "$HB_TEST_SANDBOX/tool-calls.log"\n'
                f'exec "{SHIM_DIR / name}" "$@"\n')
            shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "DISABLED").touch()
        self.state_before = _tree(self.state_dir)   # DISABLED (+ the R72 ownership marker the sandbox created)
        self.home_before = _tree(self.home)
        self.xdg_before = _tree(self.xdg_state)

    def _run_disabled(self, *argv, stdin=None, env=None):
        child_env = {"PATH": f"{self.log_shims}{os.pathsep}{os.environ['PATH']}"}
        child_env.update(env or {})
        started = time.monotonic()
        res = self.run_cli(*argv, input=stdin, env=child_env)
        return res, time.monotonic() - started

    def _assert_no_side_effects(self, res, elapsed):
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(_tree(self.state_dir), self.state_before,
                         "a DISABLED event run must not create a lock, heartbeat, log, spool or marker")
        self.assertIn("DISABLED", self.state_before)
        self.assertEqual(_tree(self.home), self.home_before)
        self.assertEqual(_tree(self.xdg_state), self.xdg_before)
        self.assertEqual(self.bridge.requests, [])
        calls = self.tool_log.read_text().splitlines() if self.tool_log.exists() else []
        self.assertEqual(calls, [], "no ps/pgrep/osascript/herdr call may run before the DISABLED exit")
        self.assertEqual(self.subprocess_spawns(), [])
        self.assertLess(elapsed, DISABLED_EXIT_BUDGET_SECONDS)

    def test_stdin_status_event_is_a_true_noop(self):
        """Plan §10.1 #14: with DISABLED present a real stdin status envelope (which would admit a session
        and POST to the bridge) exits 0 before the identity warm-up, heartbeat, stdin read or cache lock."""
        res, elapsed = self._run_disabled("pane.agent_status_changed",
                                          stdin=json.dumps(STATUS_ENVELOPE).encode())
        self._assert_no_side_effects(res, elapsed)

    def test_stdin_close_event_is_a_true_noop(self):
        """Plan §10.1 #14: with DISABLED present a pane.closed envelope exits 0 without any state or bridge effect."""
        res, elapsed = self._run_disabled("pane.closed", stdin=json.dumps(CLOSE_ENVELOPE).encode())
        self._assert_no_side_effects(res, elapsed)

    def test_env_event_is_a_true_noop(self):
        """Plan §10.1 #14: with DISABLED present the HERDR_PLUGIN_EVENT env form is also a no-op."""
        res, elapsed = self._run_disabled(env={"HERDR_PLUGIN_EVENT": "pane.agent_status_changed",
                                               "HERDR_PLUGIN_EVENT_JSON": json.dumps(STATUS_ENVELOPE)})
        self._assert_no_side_effects(res, elapsed)

    def test_control_without_disabled_the_same_event_has_effects(self):
        """Plan §10.1 #14 (control): the same stdin status event without DISABLED admits and delivers, so the
        no-op assertions above are not vacuous."""
        (self.state_dir / "DISABLED").unlink()
        res, _ = self._run_disabled("pane.agent_status_changed", stdin=json.dumps(STATUS_ENVELOPE).encode())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("active-sessions.lock", _tree(self.state_dir))
        self.assertTrue(self.bridge.posts(), "the enabled path must POST the Waiting session")
        self.assertTrue(self.tool_log.exists() and self.tool_log.read_text().strip(),
                        "the enabled path warms its identity with ps")


if __name__ == "__main__":
    unittest.main()
