"""Unit and CLI tests for local snapshot self-healing and T5 CLI/manifest wiring."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from herdr_bartender.agy_hook import AGY_HOOK_NAME, PRE_TOOL_USE_WARNING, agy_source_path
from herdr_bartender.handlers import handle_agent_status_changed
from herdr_bartender.herdr_client import fetch_herdr_snapshot
from herdr_bartender.hooks import default_agy_hooks_config_path
from herdr_bartender.reconciler import reconcile_active_sessions
from herdr_bartender.remote_watcher import SnapshotBundle, reset_watcher_timers, sync_local_snapshot
from tests.support import SandboxTestCase
from tests.support.reconciler_fixtures import pane_file, read_cache

REVIEW_WARNING = (
    "[WARNING] Vendor hook modified upstream (SHA mismatch). "
    "Run 'herdr-bartender --install-hooks' to re-verify and approve changes."
)


class LocalSnapshotSelfHealingTests(SandboxTestCase):
    start_bridge = True

    def setUp(self) -> None:
        super().setUp()
        self.clock = self.use_fake_clock(start=1_750_010_000.0)
        reset_watcher_timers()
        self.addCleanup(reset_watcher_timers)

    def _local_bundle(self, snap: dict, *, fetched_ns: int | None = None, fetched_wall: float | None = None) -> SnapshotBundle:
        return SnapshotBundle(
            local_snapshot=snap,
            remote_snapshots=(),
            remotes_configured=False,
            fetched_at_wall=self.clock.time() if fetched_wall is None else fetched_wall,
            fetched_at_ns=self.clock.time_ns() if fetched_ns is None else fetched_ns,
        )

    def test_fetch_herdr_snapshot_returns_none_on_empty_shim_stdout_and_parses_json(self) -> None:
        # Default sandbox shim outputs "" and exits 0 -> must return None (never a 0-pane snapshot!).
        self.assertIsNone(fetch_herdr_snapshot(None))
        herdr_log = self.sandbox / "herdr.log"
        self.assertTrue(herdr_log.exists())
        self.assertIn("api snapshot", herdr_log.read_text(encoding="utf-8"))

    def test_sync_local_snapshot_refreshes_frozen_cwd_and_title_via_foreground_cwd(self) -> None:
        handle_agent_status_changed(
            {
                "pane_id": "w18:p1",
                "workspace_id": "w18",
                "tab_id": "w18:t1",
                "agent": "agy",
                "agent_status": "working",
                "title": "voodoo-voice",
                "cwd": "/Users/voodoo/Projects/voodoo-voice",
            },
            {},
            bridge_url=self.mock_url,
        )
        sid = self.sid("w18:p1")
        self.assertEqual(read_cache(self.cache_mgr)["sessions"][sid]["seq"], 1)
        self.bridge.requests.clear()

        self.clock.advance(10.0)
        snap = {
            "focused_pane_id": "w18:p1",
            "workspaces": [{"workspace_id": "w18", "label": "Herdr Bartender"}],
            "panes": [
                {
                    "pane_id": "w18:p1",
                    "workspace_id": "w18",
                    "tab_id": "w18:t2",
                    "agent": "agy",
                    "agent_status": "working",
                    "title": "herdr-bartender",
                    "cwd": "/Users/voodoo/Projects/voodoo-voice",
                    "foreground_cwd": "/Users/voodoo/Projects/herdr-bartender",
                }
            ],
        }
        reconcile_active_sessions(
            self.state_dir,
            bridge_url=self.mock_url,
            snapshots=self._local_bundle(snap),
        )

        posts = self.bridge.posts()
        self.assertEqual(len(posts), 1, posts)
        self.assertEqual(posts[0]["session_id"], sid)
        self.assertEqual(posts[0]["cwd"], "/Users/voodoo/Projects/herdr-bartender")
        self.assertEqual(posts[0]["title"], "herdr-bartender")
        self.assertEqual(posts[0]["state"], "Working")
        self.assertEqual(posts[0]["seq"], 2)

        updated = read_cache(self.cache_mgr)["sessions"][sid]
        self.assertEqual(updated["cwd"], "/Users/voodoo/Projects/herdr-bartender")
        self.assertEqual(updated["title"], "herdr-bartender")
        self.assertEqual(updated["tab_id"], "w18:t2")
        self.assertEqual(updated["seq"], 2)
        self.assertEqual(updated["delivery_status"], "delivered")

        # Subsequent identical snapshot is a pure no-op (zero extra bridge POSTs).
        self.bridge.requests.clear()
        self.clock.advance(10.0)
        reconcile_active_sessions(
            self.state_dir,
            bridge_url=self.mock_url,
            snapshots=self._local_bundle(snap),
        )
        self.assertEqual(self.bridge.posts(), [])
        self.assertEqual(read_cache(self.cache_mgr)["sessions"][sid]["seq"], 2)

    def test_sync_local_snapshot_closes_missing_pane_but_preserves_session_admitted_after_snapshot_ns(self) -> None:
        # Admit w1:pGone BEFORE snapshot_ns
        handle_agent_status_changed(
            {
                "pane_id": "w1:pGone",
                "workspace_id": "w1",
                "agent": "claude",
                "agent_status": "working",
                "title": "Old pane",
                "cwd": "/tmp/old",
            },
            {},
            bridge_url=self.mock_url,
        )
        gone_sid = self.sid("w1:pGone")
        self.assertTrue(pane_file(self.state_dir, "w1:pGone").exists())

        # Snapshot begins at t+5s (neither w1:pGone nor w1:pRaceNew is in snapshot["panes"])
        self.clock.advance(5.0)
        snapshot_wall = self.clock.time()
        snapshot_ns = self.clock.time_ns()

        # While snapshot is in flight (at t+6s > snapshot_ns), w1:pRaceNew is admitted!
        self.clock.advance(1.0)
        handle_agent_status_changed(
            {
                "pane_id": "w1:pRaceNew",
                "workspace_id": "w1",
                "agent": "agy",
                "agent_status": "working",
                "title": "Brand new pane",
                "cwd": "/tmp/new",
            },
            {},
            bridge_url=self.mock_url,
        )
        new_sid = self.sid("w1:pRaceNew")
        self.bridge.requests.clear()

        reconcile_active_sessions(
            self.state_dir,
            bridge_url=self.mock_url,
            snapshots=self._local_bundle({"panes": []}, fetched_ns=snapshot_ns, fetched_wall=snapshot_wall),
        )

        posts = self.bridge.posts()
        self.assertEqual([p["session_id"] for p in posts], [gone_sid])
        self.assertEqual(posts[0]["state"], "Ended")
        self.assertFalse(pane_file(self.state_dir, "w1:pGone").exists())

        sessions = read_cache(self.cache_mgr)["sessions"]
        self.assertNotIn(gone_sid, sessions)
        self.assertIn(new_sid, sessions, "session admitted after snapshot_ns must NOT be closed")
        self.assertEqual(sessions[new_sid]["desired_state"], "Working")
        self.assertTrue(pane_file(self.state_dir, "w1:pRaceNew").exists())

    def test_sync_local_snapshot_closes_session_when_pane_returns_to_idle_shell_without_agent(self) -> None:
        handle_agent_status_changed(
            {
                "pane_id": "w2:p1",
                "workspace_id": "w2",
                "agent": "agy",
                "agent_status": "working",
                "title": "Agent run",
            },
            {},
            bridge_url=self.mock_url,
        )
        sid = self.sid("w2:p1")
        self.bridge.requests.clear()
        self.clock.advance(10.0)

        snap = {"panes": [{"pane_id": "w2:p1", "workspace_id": "w2", "agent": None, "agent_status": "idle"}]}
        reconcile_active_sessions(
            self.state_dir,
            bridge_url=self.mock_url,
            snapshots=self._local_bundle(snap),
        )
        posts = self.bridge.posts()
        self.assertEqual(len(posts), 1)
        self.assertEqual((posts[0]["session_id"], posts[0]["state"]), (sid, "Ended"))
        self.assertNotIn(sid, read_cache(self.cache_mgr)["sessions"])


class CliManifestAndHookWiringTests(SandboxTestCase):
    start_bridge = True

    def test_cli_dispatches_pane_moved_and_pane_exited(self) -> None:
        # 1. Admit initial session on w1:p1
        admit = {
            "event": "pane.agent_status_changed",
            "data": {
                "pane_id": "w1:p1",
                "workspace_id": "w1",
                "tab_id": "w1:t1",
                "agent": "agy",
                "agent_status": "working",
                "title": "Moving task",
                "cwd": "/Users/test/repo",
            },
            "context": {},
        }
        res = self.run_cli("pane.agent_status_changed", input=json.dumps(admit).encode())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(self.sid("w1:p1"), read_cache(self.cache_mgr)["sessions"])

        # 2. Dispatch pane.moved (w1:p1 -> w2:p1) via CLI
        moved = {
            "event": "pane.moved",
            "data": {
                "previous_pane_id": "w1:p1",
                "previous_workspace_id": "w1",
                "pane": {
                    "pane_id": "w2:p1",
                    "workspace_id": "w2",
                    "tab_id": "w2:t1",
                    "agent": "agy",
                    "agent_status": "working",
                    "title": "Moved task",
                    "foreground_cwd": "/Users/test/repo-v2",
                },
            },
            "context": {},
        }
        res = self.run_cli("pane.moved", input=json.dumps(moved).encode())
        self.assertEqual(res.returncode, 0, res.stderr)
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        sessions = read_cache(self.cache_mgr)["sessions"]
        self.assertNotIn(self.sid("w1:p1"), sessions)
        self.assertIn(self.sid("w2:p1"), sessions)
        self.assertEqual(sessions[self.sid("w2:p1")]["cwd"], "/Users/test/repo-v2")

        # 3. Dispatch pane.exited on w2:p1 via CLI
        exited = {
            "event": "pane.exited",
            "data": {"pane_id": "w2:p1", "workspace_id": "w2"},
            "context": {},
        }
        res = self.run_cli("pane.exited", input=json.dumps(exited).encode())
        self.assertEqual(res.returncode, 0, res.stderr)
        sessions_after = read_cache(self.cache_mgr)["sessions"]
        self.assertNotIn(self.sid("w2:p1"), sessions_after)
        ended_events = [e for e in self.bridge.events_for(self.sid("w2:p1")) if e.get("state") == "Ended"]
        self.assertTrue(ended_events)
        self.assertEqual(ended_events[-1].get("event"), "pane.exited")

    def test_install_agy_hook_and_health_reporting(self) -> None:
        self.vendor_hooks_dir.mkdir(parents=True, exist_ok=True)
        # Before installing: agy_hook_intact is False ("missing")
        h1 = json.loads(self.run_cli("--health").stdout)
        self.assertIs(h1["agy_hook_intact"], False)
        self.assertEqual(h1["agy_hook_status"], "missing")
        self.assertNotIn("agy_hooks_json_warnings", h1)

        # Install via --install-agy-hook
        inst = self.run_cli("--install-agy-hook")
        self.assertEqual(inst.returncode, 0, inst.stdout + inst.stderr)
        deployed = self.vendor_hooks_dir / AGY_HOOK_NAME
        self.assertEqual(deployed.read_bytes(), agy_source_path().read_bytes())

        # Seed PreToolUse warning in hooks.json and verify --health reports intact + warning
        cfg_path: Path = default_agy_hooks_config_path()
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(
            json.dumps({"hooks": {"PreToolUse": [{"command": f"/path/to/{AGY_HOOK_NAME}"}]}}),
            encoding="utf-8",
        )
        h2 = json.loads(self.run_cli("--health").stdout)
        self.assertIs(h2["agy_hook_intact"], True)
        self.assertEqual(h2["agy_hook_status"], "intact")
        self.assertEqual(h2["agy_hooks_json_warnings"], [PRE_TOOL_USE_WARNING])

    def test_status_reports_bridge_drift_and_preserves_review_warning_on_line_zero(self) -> None:
        # Inject 2 phantom zombie sessions directly on MockBridge (0 tracked locally) + HOOK_NEEDS_REVIEW
        self.bridge.sessions["phantom-1"] = {"state": "Working"}
        self.bridge.sessions["phantom-2"] = {"state": "Working"}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "HOOK_NEEDS_REVIEW").touch()

        cfg_path: Path = default_agy_hooks_config_path()
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(
            json.dumps({"hooks": {"PreToolUse": [{"command": AGY_HOOK_NAME}]}}),
            encoding="utf-8",
        )

        res = self.run_cli("--status")
        self.assertEqual(res.returncode, 0, res.stderr)
        lines = res.stdout.splitlines()
        self.assertEqual(lines[0], REVIEW_WARNING)
        self.assertIn(f"[WARNING] {PRE_TOOL_USE_WARNING}", lines)
        self.assertIn("Bridge sessions: 2 (tracked delivered: 0)", lines)


if __name__ == "__main__":
    unittest.main()
