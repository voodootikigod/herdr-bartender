"""Unit and integration tests for Option R1 remote SSH watcher (herdr_client + remote_watcher)."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from herdr_bartender import runtime
from herdr_bartender.background import run_reconcile_background
from herdr_bartender.handlers import handle_agent_status_changed
from herdr_bartender.herdr_client import (
    RemoteEndpoint,
    discover_enabled_remotes,
    endpoints_file_path,
    fetch_herdr_snapshot,
)
from herdr_bartender.markers import touch_pane_marker
from herdr_bartender.reconciler import reconcile_active_sessions
from herdr_bartender.remote_watcher import (
    REMOTE_POLL_SECONDS,
    REMOTE_UNREACHABLE_GRACE_SECONDS,
    SnapshotBundle,
    collect_snapshots,
    reset_watcher_timers,
    sync_remote_endpoint,
)
from herdr_bartender.schedule import cache_view
from tests.support import SandboxTestCase
from tests.support.reconciler_fixtures import LoopRunner, pane_file, read_cache


def _remote_4_agent_snapshot(*, include_p2: bool = True) -> dict:
    panes = [
        {
            "pane_id": "w18:p1",
            "workspace_id": "w18",
            "tab_id": "w18:t1",
            "agent": "agy",
            "agent_status": "working",
            "title": "ADLC T1 multihost",
            "foreground_cwd": "/home/dev/herdr-bartender",
        },
        {
            "pane_id": "w1K:p1",
            "workspace_id": "w1K",
            "tab_id": "w1K:t1",
            "agent": "agy",
            "agent_status": "working",
            "title": "ADLC T3 agy-hook",
            "foreground_cwd": "/home/dev/herdr-bartender",
        },
        {
            "pane_id": "w1J:p1",
            "workspace_id": "w1J",
            "tab_id": "w1J:t1",
            "agent": "claude",
            "agent_status": "idle",
            "title": "Interactive shell",
            "cwd": "/home/dev/voodoo",
        },
    ]
    if include_p2:
        panes.insert(
            1,
            {
                "pane_id": "w1E:p2",
                "workspace_id": "w1E",
                "tab_id": "w1E:t1",
                "agent": "agy",
                "agent_status": "working",
                "title": "ADLC T2 reconciler",
                "foreground_cwd": "/home/dev/herdr-bartender",
            },
        )
    return {
        "focused_pane_id": "w18:p1",
        "workspaces": [
            {"workspace_id": "w18", "label": "Fleet 1"},
            {"workspace_id": "w1E", "label": "Fleet 2"},
            {"workspace_id": "w1K", "label": "Fleet 3"},
            {"workspace_id": "w1J", "label": "Ops"},
        ],
        "panes": panes,
    }


class EndpointDiscoveryTests(SandboxTestCase):
    start_bridge = False

    def setUp(self) -> None:
        super().setUp()
        reset_watcher_timers()
        self.addCleanup(reset_watcher_timers)

    def _write_endpoints(self, payload: object) -> Path:
        path = endpoints_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_missing_endpoints_returns_empty_with_zero_subprocesses(self) -> None:
        with mock.patch("herdr_bartender.herdr_client.subprocess.run", side_effect=AssertionError("spawned")):
            self.assertEqual(discover_enabled_remotes(), ())
        herdr_log = self.sandbox / "herdr.log"
        self.assertFalse(herdr_log.exists())

    def test_discovers_enabled_entries_sanitizes_host_and_rejects_bad_targets(self) -> None:
        self._write_endpoints(
            {
                "ssh": [
                    {
                        "id": "ep-dev-one",
                        "target": "dev-one.taildd69d8.ts.net",
                        "label": "",
                        "enabled": True,
                    },
                    {
                        "id": "ep-disabled",
                        "target": "staging.taildd69d8.ts.net",
                        "label": "staging",
                        "enabled": False,
                    },
                    {
                        "id": "ep-inject-target",
                        "target": "-oProxyCommand=evil",
                        "label": "evil",
                        "enabled": True,
                    },
                    {
                        "id": "-bad-id",
                        "enabled": True,
                    },
                    {
                        "id": "ep-space",
                        "target": "bad host",
                        "enabled": True,
                    },
                    {
                        "id": "ep-dup",
                        "target": "dev-one.other.domain",
                        "label": "dev-one",
                        "enabled": True,
                    },
                    {
                        "id": "ep-two",
                        "target": "100.64.1.22",
                        "label": "Build-Box.local",
                        "enabled": True,
                    },
                ]
            }
        )
        remotes = discover_enabled_remotes()
        self.assertEqual(
            remotes,
            (
                RemoteEndpoint(
                    id="ep-dev-one",
                    host="dev-one",
                    machine_arg="dev-one.taildd69d8.ts.net",
                    label="",
                ),
                RemoteEndpoint(
                    id="ep-two",
                    host="build-box",
                    machine_arg="100.64.1.22",
                    label="Build-Box.local",
                ),
            ),
        )

    def test_symlink_endpoints_file_is_rejected(self) -> None:
        real_file = self.tmp / "outside-endpoints.json"
        real_file.write_text(
            json.dumps({"ssh": [{"id": "ep1", "target": "dev-one", "enabled": True}]}),
            encoding="utf-8",
        )
        ep_path = endpoints_file_path()
        ep_path.parent.mkdir(parents=True, exist_ok=True)
        ep_path.symlink_to(real_file)
        self.assertEqual(discover_enabled_remotes(), ())

    def test_lock_discipline_raises_if_called_in_critical_section(self) -> None:
        with self.cache_mgr:
            self.assertTrue(runtime.IN_CRITICAL_SECTION)
            with self.assertRaises(AssertionError):
                discover_enabled_remotes()
            with self.assertRaises(AssertionError):
                fetch_herdr_snapshot("dev-one")
            with self.assertRaises(AssertionError):
                collect_snapshots(herdr_alive=True)

    def test_fetch_herdr_snapshot_rejects_option_injection(self) -> None:
        with mock.patch("herdr_bartender.herdr_client.subprocess.run", side_effect=AssertionError("spawned")):
            self.assertIsNone(fetch_herdr_snapshot("-bad"))
            self.assertIsNone(fetch_herdr_snapshot(""))


class RemoteWatcherEndToEndTests(SandboxTestCase):
    start_bridge = True

    def setUp(self) -> None:
        super().setUp()
        self.clock = self.use_fake_clock(start=1_750_000_000.0)
        reset_watcher_timers()
        self.addCleanup(reset_watcher_timers)
        self.dev_one = RemoteEndpoint(
            id="ep-dev-one",
            host="dev-one",
            machine_arg="dev-one.taildd69d8.ts.net",
            label="",
        )

    def _bundle(self, snap: dict | None) -> SnapshotBundle:
        return SnapshotBundle(
            local_snapshot=None,
            remote_snapshots=((self.dev_one, snap),),
            remotes_configured=True,
            fetched_at_wall=self.clock.time(),
            fetched_at_ns=self.clock.time_ns(),
        )

    def test_remote_lifecycle_delivery_zero_churn_flap_tolerance_and_local_isolation(self) -> None:
        # Seed a live local session on overlapping pane_id w18:p1 to verify zero cross-host interference.
        handle_agent_status_changed(
            {
                "pane_id": "w18:p1",
                "workspace_id": "w18",
                "tab_id": "w18:t1",
                "agent": "claude",
                "agent_status": "working",
                "title": "Local work",
                "cwd": "/Users/local/proj",
            },
            {},
            bridge_url=self.mock_url,
        )
        local_sid = self.sid("w18:p1")
        local_marker = pane_file(self.state_dir, "w18:p1")
        self.assertTrue(local_marker.exists(), "local session has its local pane marker")
        panes_dir = self.state_dir / "panes"
        initial_marker_files = sorted(p.name for p in panes_dir.iterdir())
        self.bridge.requests.clear()
        self.bridge.history.clear()

        # 1. Initial poll with 4 remote agents on dev-one.taildd69d8.ts.net
        self.clock.advance(5.0)
        reconcile_active_sessions(
            self.state_dir,
            bridge_url=self.mock_url,
            snapshots=self._bundle(_remote_4_agent_snapshot(include_p2=True)),
        )

        expected_sids = {
            "herdr:dev-one:w18:p1": ("Working", "Antigravity (Herdr \u00b7 dev-one)"),
            "herdr:dev-one:w1E:p2": ("Working", "Antigravity (Herdr \u00b7 dev-one)"),
            "herdr:dev-one:w1K:p1": ("Working", "Antigravity (Herdr \u00b7 dev-one)"),
            "herdr:dev-one:w1J:p1": ("Idle", "Claude (Herdr \u00b7 dev-one)"),
        }
        posts = self.bridge.posts()
        self.assertEqual(len(posts), 4, posts)
        by_sid = {p["session_id"]: p for p in posts}
        self.assertEqual(set(by_sid), set(expected_sids))
        for rsid, (exp_state, exp_agent) in expected_sids.items():
            payload = by_sid[rsid]
            self.assertEqual(payload["state"], exp_state)
            self.assertEqual(payload["agent"], exp_agent)
            self.assertEqual(payload["terminal"], "Herdr")
            self.assertEqual(payload["seq"], 1)

        # Remote deliveries touch ZERO local panes/<hex> marker files!
        self.assertEqual(sorted(p.name for p in panes_dir.iterdir()), initial_marker_files)

        # Heartbeat eligibility in schedule.cache_view excludes remote sessions.
        cache_now = read_cache(self.cache_mgr)
        view = cache_view(cache_now, self.clock.time(), True)
        self.assertEqual(view.heartbeat_panes, ("w18:p1",))

        # 2. Second identical snapshot poll produces ZERO additional bridge POSTs (seq stays 1).
        self.bridge.requests.clear()
        self.clock.advance(3.0)
        reconcile_active_sessions(
            self.state_dir,
            bridge_url=self.mock_url,
            snapshots=self._bundle(_remote_4_agent_snapshot(include_p2=True)),
        )
        self.assertEqual(self.bridge.posts(), [])
        cached = read_cache(self.cache_mgr)["sessions"]
        for rsid in expected_sids:
            self.assertEqual(cached[rsid]["seq"], 1)
            self.assertEqual(cached[rsid]["delivery_status"], "delivered")

        # 3. Transient SSH disconnect at t+10s and t+250s records remote_unreachable_since and sends ZERO Endeds.
        self.clock.advance(10.0)
        disconnect_t0 = self.clock.time()
        reconcile_active_sessions(
            self.state_dir,
            bridge_url=self.mock_url,
            snapshots=self._bundle(None),
        )
        self.assertEqual(self.bridge.posts(), [])
        self.assertEqual(read_cache(self.cache_mgr)["remote_unreachable_since"].get("dev-one"), disconnect_t0)

        self.clock.advance(240.0)  # t+250s total (240s since disconnect <= 300s grace)
        reconcile_active_sessions(
            self.state_dir,
            bridge_url=self.mock_url,
            snapshots=self._bundle(None),
        )
        self.assertEqual(self.bridge.posts(), [])
        for rsid in expected_sids:
            self.assertNotEqual(read_cache(self.cache_mgr)["sessions"][rsid]["desired_state"], "Ended")

        # 4. Reconnect at t+260s with w1E:p2 closed sends Ended ONLY for herdr:dev-one:w1E:p2 and clears unreachable.
        self.clock.advance(10.0)
        reconcile_active_sessions(
            self.state_dir,
            bridge_url=self.mock_url,
            snapshots=self._bundle(_remote_4_agent_snapshot(include_p2=False)),
        )
        reconnect_posts = self.bridge.posts()
        self.assertEqual(len(reconnect_posts), 1, reconnect_posts)
        self.assertEqual(reconnect_posts[0]["session_id"], "herdr:dev-one:w1E:p2")
        self.assertEqual(reconnect_posts[0]["state"], "Ended")
        after_reconnect = read_cache(self.cache_mgr)
        self.assertNotIn("dev-one", after_reconnect["remote_unreachable_since"])
        self.assertNotIn("herdr:dev-one:w1E:p2", after_reconnect["sessions"])
        self.assertIn("dev-one@w1E:p2", after_reconnect["tombstones"])
        self.assertNotIn("w1E:p2", after_reconnect["tombstones"])

        # 5. Sustained disconnect > 300s expires all remaining live dev-one sessions while leaving local untouched!
        self.bridge.requests.clear()
        self.clock.advance(5.0)
        reconcile_active_sessions(
            self.state_dir,
            bridge_url=self.mock_url,
            snapshots=self._bundle(None),
        )
        self.assertEqual(self.bridge.posts(), [])

        self.clock.advance(REMOTE_UNREACHABLE_GRACE_SECONDS + 5.0)  # 305s unreachable > 300s grace
        reconcile_active_sessions(
            self.state_dir,
            bridge_url=self.mock_url,
            snapshots=self._bundle(None),
        )
        expired_posts = self.bridge.posts()
        self.assertEqual(
            {p["session_id"] for p in expired_posts},
            {"herdr:dev-one:w18:p1", "herdr:dev-one:w1K:p1", "herdr:dev-one:w1J:p1"},
        )
        self.assertTrue(all(p["state"] == "Ended" for p in expired_posts))

        final_cache = read_cache(self.cache_mgr)
        self.assertEqual(set(final_cache["sessions"]), {local_sid})
        self.assertEqual(final_cache["sessions"][local_sid]["desired_state"], "Working")
        self.assertTrue(local_marker.exists(), "local w18:p1 marker stays intact after remote w18:p1 expired")

    def test_background_loop_polls_configured_remotes_every_3s_without_premature_idle_exit(self) -> None:
        ep_path = endpoints_file_path()
        ep_path.parent.mkdir(parents=True, exist_ok=True)
        ep_path.write_text(
            json.dumps({"ssh": [{"id": "ep1", "target": "dev-one.taildd69d8.ts.net", "enabled": True}]}),
            encoding="utf-8",
        )
        shim_dir = self.tmp / "herdr-json-shim"
        shim_dir.mkdir(parents=True, exist_ok=True)
        herdr_bin = shim_dir / "herdr"
        snap_json = json.dumps({"ok": True, "result": {"snapshot": {"panes": [], "workspaces": []}}})
        herdr_bin.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$*" >> "$HB_TEST_SANDBOX/herdr.log"\n'
            f"printf '%s\\n' '{snap_json}'\n",
            encoding="utf-8",
        )
        herdr_bin.chmod(0o755)
        os.environ["PATH"] = f"{shim_dir}{os.pathsep}{os.environ['PATH']}"

        start = self.clock.time()
        runner = LoopRunner(self.state_dir, stop=lambda: self.clock.time() - start >= 70.0, max_passes=50)
        runner.run(bridge_url=self.mock_url)

        # Past the 60s idle window, the loop kept running until our 70s stop condition (did NOT idle-exit!).
        self.assertGreaterEqual(self.clock.time() - start, 70.0)
        self.assertTrue((self.state_dir / "DISABLED").exists(), "stopped by LoopRunner at 70s, not idle exit")
        sweeps = runner.sweep_times()
        gaps = [round(b - a, 1) for a, b in zip(sweeps, sweeps[1:6])]
        self.assertEqual(gaps, [REMOTE_POLL_SECONDS] * 5)
        herdr_calls = (self.sandbox / "herdr.log").read_text(encoding="utf-8").splitlines()
        self.assertTrue(
            all(line == "--machine dev-one.taildd69d8.ts.net api snapshot" for line in herdr_calls),
            herdr_calls,
        )


if __name__ == "__main__":
    unittest.main()
