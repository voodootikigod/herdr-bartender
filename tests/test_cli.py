"""bin/herdr-bartender launcher smoke tests (subprocess, sandboxed)."""

import json
import os
import socket
import subprocess
import sys
import time
import unittest
from unittest import mock

from herdr_bartender import cli, handoff, intake
from herdr_bartender.markers import touch_delivery_down, touch_pane_marker
from tests.support import LAUNCHER, REPO_ROOT, SandboxTestCase

STATUS_FIXTURE = {
    "event": "pane.agent_status_changed",
    "data": {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1", "agent": "claude",
             "agent_status": "blocked", "title": "Reviewing pull request #42", "timestamp": 1727998410.12},
    "context": {"focused_pane_id": "w1:p1", "focused_pane_agent": "claude", "focused_pane_cwd": "/workspace",
                "workspace_id": "w1", "workspace_label": "Dev", "tab_id": "w1:t1"},
}


def _load_manifest(path):
    """Tiny reader for the manifest's TOML subset (tables, arrays of tables, JSON-compatible values)."""
    root, current = {}, None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[["):
            current = {}
            root.setdefault(line.strip("[]"), []).append(current)
        elif line.startswith("["):
            current = root.setdefault(line.strip("[]"), {})
        else:
            key, _, value = line.partition("=")
            (current if current is not None else root)[key.strip()] = json.loads(value.strip())
    return root


class LauncherTests(SandboxTestCase):
    start_bridge = False

    def test_launcher_is_executable(self):
        """The launcher stays an executable python3 script."""
        self.assertTrue(os.access(LAUNCHER, os.X_OK))
        self.assertTrue(LAUNCHER.read_text().startswith("#!/usr/bin/env python3"))

    def test_launcher_resolves_through_symlink(self):
        """A symlinked launcher (as in ~/.config/herdr/plugins) still finds the package."""
        plugin_dir = self.home / ".config" / "herdr" / "plugins" / "local"
        plugin_dir.mkdir(parents=True)
        link = plugin_dir / "herdr-bartender"
        link.symlink_to(LAUNCHER)
        res = subprocess.run([sys.executable, str(link), "--sessions"], capture_output=True, text=True,
                             env=dict(os.environ), timeout=15)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout)["sessions"], {})
        self.assertTrue((self.state_dir / "active-sessions.lock").exists(), "CLI must use the sandboxed state dir")

    def test_disabled_event_dispatch_is_noop(self):
        """Plan §10.1 #14 (CLI path): with DISABLED present, an event invocation exits 0 without touching the cache."""
        (self.state_dir / "DISABLED").touch()
        res = self.run_cli(env={"HERDR_PLUGIN_EVENT": "pane.closed",
                                "HERDR_PLUGIN_EVENT_JSON": json.dumps({"data": {"pane_id": "w1:p1"}})})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse((self.state_dir / "active-sessions.json").exists())

    def test_replay_orphans_usage(self):
        """--replay-orphans without a path prints usage and exits 1."""
        res = self.run_cli("--replay-orphans")
        self.assertEqual(res.returncode, 1)
        self.assertIn("Usage: --replay-orphans", res.stdout)

    # -- R20 invocation contract ----------------------------------------------------
    def _event_proc(self, argv, stdin_bytes):
        self.add_fake_process("Bartender 6", pid=424242)
        return self.run_cli(*argv, input=stdin_bytes)

    def test_stdin_envelope_dispatches_fixture(self):
        """Plan §3.4 / §11.1 / R20 (gap event-input-contract): the §11.1 envelope on stdin is dispatched."""
        res = self._event_proc(["pane.agent_status_changed"], json.dumps(STATUS_FIXTURE).encode())
        self.assertEqual(res.returncode, 0, res.stderr)
        with self.cache_mgr as data:
            s = data["sessions"][self.sid("w1:p1")]
            self.assertEqual((s["desired_state"], s["tab_id"], s["title"]), ("Waiting", "w1:t1", "Reviewing pull request #42"))

    def test_stdin_envelope_close_fixtures(self):
        """Plan §11.1 / R20 (gap main-stdin-envelope): pane.closed via stdin with only the argv event name ends the session."""
        self._event_proc(["pane.agent_status_changed"], json.dumps(STATUS_FIXTURE).encode())
        close = {"data": {"pane_id": "w1:p1", "workspace_id": "w1"}}
        res = self.run_cli("pane.closed", input=json.dumps(close).encode())
        self.assertEqual(res.returncode, 0, res.stderr)
        with self.cache_mgr as data:
            s = data["sessions"].get(self.sid("w1:p1"))
            self.assertTrue(s is None or s["desired_state"] == "Ended", s)
            self.assertIn("w1:p1", data["tombstones"])

    def test_empty_and_malformed_stdin_are_safe(self):
        """R20 (gap event-input-contract): empty/malformed stdin with a known argv event exits 0 without touching the cache."""
        for raw in (b"", b"{garbage", b"\xff" * 10):
            with self.subTest(raw=raw):
                res = self.run_cli("pane.closed", input=raw)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertFalse((self.state_dir / "active-sessions.json").exists())
        self.assertIn("no usable payload for 'pane.closed'", (self.state_dir / "plugin.log").read_text())

    def test_payloadless_known_event_starts_reconciler(self):
        """R20 (gap event-input-contract): a known argv event without payload calls ensure_reconciler_running()."""
        with mock.patch.object(cli, "dispatch_event") as dispatch:
            self.assertEqual(cli.run_event(["tab.closed"], b"", {}), 0)
            self.assertEqual(self.spawner.calls, [handoff.reconciler_argv()])
            dispatch.assert_not_called()
            self.spawner.reset()
            cli.run_event(["not.an.event"], b"", {})
            cli.run_event([], b"", {})
            self.assertEqual(self.spawner.calls, [])
            dispatch.assert_not_called()

    def test_run_event_dispatches_parsed_payload(self):
        """R20 (gap event-input-contract): run_event hands the stdin envelope's data/context to the dispatcher."""
        with mock.patch.object(cli, "dispatch_event") as dispatch:
            cli.run_event(["pane.closed"], json.dumps(STATUS_FIXTURE).encode(), {})
            dispatch.assert_called_once_with("pane.agent_status_changed", STATUS_FIXTURE["data"], STATUS_FIXTURE["context"])

    def test_legacy_env_still_dispatches(self):
        """R20 (gap event-input-contract): legacy HERDR_PLUGIN_EVENT* env vars remain a fallback."""
        self.add_fake_process("Bartender 6", pid=424242)
        env = {"HERDR_PLUGIN_EVENT": "pane.agent_status_changed",
               "HERDR_PLUGIN_EVENT_JSON": json.dumps({"data": STATUS_FIXTURE["data"]})}
        res = self.run_cli(env=env, input=b"")
        self.assertEqual(res.returncode, 0, res.stderr)
        with self.cache_mgr as data:
            self.assertIn(self.sid("w1:p1"), data["sessions"])

    def test_dispatch_table_covers_event_names(self):
        """Plan §2.1 (gap manifest-no-argv-event): every intake event name has a CLI handler."""
        self.assertEqual(set(cli.EVENT_HANDLERS), set(intake.EVENT_NAMES))

    # -- R21 manifest ---------------------------------------------------------------
    def test_manifest_events_pass_event_name(self):
        """R21 / Plan §3.4 (gap manifest-no-argv-event): each [[events]] command passes its event name as argv[1]."""
        manifest = _load_manifest(REPO_ROOT / "herdr-plugin.toml")
        events = manifest["events"]
        self.assertEqual({e["on"] for e in events}, set(intake.EVENT_NAMES))
        for entry in events:
            with self.subTest(event=entry["on"]):
                self.assertEqual(entry["command"], ["./bin/herdr-bartender", entry["on"]])
                self.assertIn(entry["command"][1], cli.EVENT_HANDLERS)

    def test_manifest_startup_runs_reconcile_background(self):
        """R21 (gap manifest-no-argv-event): the startup hook runs --reconcile-background, never --cleanup."""
        startup = _load_manifest(REPO_ROOT / "herdr-plugin.toml")["startup"]
        self.assertEqual([s["command"] for s in startup], [["./bin/herdr-bartender", "--reconcile-background"]])


    # -- R21 startup detach ---------------------------------------------------------
    def _seed_loop_keepalive_state(self):
        """State that keeps the reconciler loop alive: an active delivered session, an in_flight one, DELIVERY_DOWN."""
        with self.cache_mgr as data:
            data["sessions"][self.sid("w1:p1")] = {
                "desired_state": "Waiting", "delivered_state": "Waiting", "seq": 1, "delivered_seq": 1,
                "delivery_status": "delivered", "last_event_at": time.time(), "pane_id": "w1:p1",
                "agent": "Claude (Herdr)",
            }
            data["sessions"][self.sid("w1:p2")] = {
                "desired_state": "Working", "seq": 2, "delivered_seq": 1, "delivery_status": "in_flight",
                "last_event_at": time.time(), "pane_id": "w1:p2", "agent": "Claude (Herdr)",
            }
            self.cache_mgr.save(data)
        touch_pane_marker("w1:p1")
        touch_delivery_down()

    @staticmethod
    def _closed_port() -> str:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return str(s.getsockname()[1])

    def test_startup_reconcile_background_returns_within_budget(self):
        """R21 (gap manifest-no-argv-event): the startup command detaches, so it exits well inside the 2.0s budget
        even while an in_flight session would keep the reconciler loop alive."""
        self._seed_loop_keepalive_state()
        started = time.monotonic()
        try:
            res = self.run_cli("--reconcile-background", timeout=8,
                               env={"NOTCHBAR_AGENTS_PORT": self._closed_port()})
        except subprocess.TimeoutExpired:
            self.fail("--reconcile-background ran the reconciler loop in the foreground")
        elapsed = time.monotonic() - started
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertLess(elapsed, 2.0, "startup hook must return inside Herdr's 2.0s supervisor budget")
        self.assertFalse((self.state_dir / "reconciler.lock").exists(),
                         "the startup process itself must not take the reconciler singleton lock")
        self.assertEqual([argv[-2:] for argv in self.subprocess_spawns()],
                         [["--reconcile-background", cli.FOREGROUND_FLAG]], "exactly one detached loop requested")


class ReconcileCommandTests(SandboxTestCase):
    """In-process checks of the --reconcile-background detach split (R21)."""

    start_bridge = False

    def setUp(self):
        super().setUp()
        previous = handoff.set_spawner(handoff.DetachedSpawner())  # the production spawner, Popen mocked below
        self.addCleanup(handoff.set_spawner, previous)

    def test_plain_invocation_spawns_detached_foreground_child(self):
        """R21 (gap manifest-no-argv-event): a plain --reconcile-background spawns one detached
        `--reconcile-background --foreground` child (own session, no inherited stdio) and returns 0."""
        with mock.patch.object(handoff.subprocess, "Popen") as popen, \
                mock.patch.object(cli, "run_reconcile_background") as loop:
            code = cli.run_reconcile_command(["--reconcile-background"])
        self.assertEqual(code, 0)
        loop.assert_not_called()
        popen.assert_called_once()
        argv = popen.call_args.args[0]
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[-2:], ["--reconcile-background", cli.FOREGROUND_FLAG])
        kwargs = popen.call_args.kwargs
        self.assertTrue(kwargs["start_new_session"])
        for stream in ("stdin", "stdout", "stderr"):
            self.assertIs(kwargs[stream], subprocess.DEVNULL, stream)

    def test_foreground_flag_runs_loop_without_respawning(self):
        """R21 (gap manifest-no-argv-event): the --foreground child runs the loop itself and never respawns."""
        with mock.patch.object(handoff.subprocess, "Popen") as popen, \
                mock.patch.object(cli, "run_reconcile_background") as loop:
            code = cli.run_reconcile_command(["--reconcile-background", cli.FOREGROUND_FLAG])
        self.assertEqual(code, 0)
        loop.assert_called_once_with()
        popen.assert_not_called()

    def test_disabled_startup_spawns_nothing(self):
        """Plan §10.1 #14 / R21 (gap manifest-no-argv-event): with DISABLED present the startup hook spawns nothing."""
        (self.state_dir / "DISABLED").touch()
        with mock.patch.object(handoff.subprocess, "Popen") as popen, \
                mock.patch.object(cli, "run_reconcile_background") as loop:
            code = cli.run_reconcile_command(["--reconcile-background"])
        self.assertEqual(code, 0)
        popen.assert_not_called()
        loop.assert_not_called()

    def test_spawn_failure_is_reported(self):
        """R21 (gap manifest-no-argv-event): a failed detach is logged and reported via a non-zero exit, not swallowed."""
        with mock.patch.object(handoff.subprocess, "Popen", side_effect=OSError("no fork")), \
                mock.patch.object(handoff, "log_debug") as log:
            code = cli.run_reconcile_command(["--reconcile-background"])
        self.assertEqual(code, 1)
        self.assertTrue(any("no fork" in str(c.args[0]) for c in log.call_args_list))


if __name__ == "__main__":
    unittest.main()
