"""Unit and integration tests for Option R1 remote SSH watcher (herdr_client + remote_watcher)."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from herdr_bartender import runtime
from herdr_bartender.cache import SESSION_CAP
from herdr_bartender.handlers import handle_agent_status_changed
from herdr_bartender.herdr_client import (
    RemoteEndpoint,
    disambiguate_remote_host,
    discover_enabled_remotes,
    endpoints_file_path,
    fetch_herdr_snapshot,
)
from herdr_bartender.intake import HOST_RE
from herdr_bartender.orphans import read_orphan_records
from herdr_bartender.paths import get_orphan_path
from herdr_bartender.reconciler import reconcile_active_sessions
from herdr_bartender.remote_watcher import (
    REMOTE_POLL_SECONDS,
    REMOTE_UNREACHABLE_GRACE_SECONDS,
    SnapshotBundle,
    collect_snapshots,
    reset_watcher_timers,
)
from herdr_bartender.schedule import cache_view
from herdr_bartender.sender.step_a import OrphanMirrorError
from tests.support import SandboxTestCase
from tests.support.reconciler_fixtures import LoopRunner, pane_file, read_cache, seed, session


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

    def test_disambiguates_remote_colliding_with_local_host(self) -> None:
        self._write_endpoints(
            {
                "ssh": [
                    {"id": "ep-same-1", "target": "10.0.0.10", "label": self.host, "enabled": True},
                    {"id": "ep-same-2", "target": "10.0.0.11", "label": self.host, "enabled": True},
                ]
            }
        )
        remotes = discover_enabled_remotes()
        self.assertEqual(len(remotes), 2)
        for ep in remotes:
            self.assertNotEqual(ep.host, self.host)
            self.assertRegex(ep.host, HOST_RE)
        self.assertNotEqual(remotes[0].host, remotes[1].host)

        # Edge case: 32-char local_host already ending in "-remote"
        tricky_local = "a" * 25 + "-remote"
        disambig = disambiguate_remote_host(tricky_local, "ep99", tricky_local)
        self.assertNotEqual(disambig, tricky_local)
        self.assertRegex(disambig, HOST_RE)

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
        self.dev_two = RemoteEndpoint(
            id="ep-dev-two",
            host="dev-two",
            machine_arg="dev-two.taildd69d8.ts.net",
            label="",
        )

    def _bundle(self, snap: dict | None) -> SnapshotBundle:
        return SnapshotBundle(
            local_snapshot=None,
            remote_snapshots=((self.dev_one, snap),),
            remotes_configured=True,
            enabled_remote_hosts=(self.dev_one.host,),
            remotes_checked=True,
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
        self.assertNotIn("dev-one", final_cache["remote_unreachable_since"])
        self.assertEqual(final_cache["sessions"][local_sid]["desired_state"], "Working")
        self.assertTrue(local_marker.exists(), "local w18:p1 marker stays intact after remote w18:p1 expired")

    def test_multi_remote_isolation_unconditional_wake_expiry_and_disabled_remote_cleanup(self) -> None:
        snap_one = {"panes": [{"pane_id": "w1:p1", "workspace_id": "w1", "agent": "agy", "agent_status": "working"}]}
        snap_two = {"panes": [{"pane_id": "w2:p1", "workspace_id": "w2", "agent": "claude", "agent_status": "working"}]}

        # Admit both dev-one and dev-two.
        reconcile_active_sessions(
            self.state_dir,
            bridge_url=self.mock_url,
            snapshots=SnapshotBundle(
                remote_snapshots=((self.dev_one, snap_one), (self.dev_two, snap_two)),
                remotes_configured=True,
                enabled_remote_hosts=("dev-one", "dev-two"),
                remotes_checked=True,
                fetched_at_wall=self.clock.time(),
                fetched_at_ns=self.clock.time_ns(),
            ),
        )
        self.assertEqual(
            set(read_cache(self.cache_mgr)["sessions"]),
            {"herdr:dev-one:w1:p1", "herdr:dev-two:w2:p1"},
        )
        self.bridge.requests.clear()

        # Disconnect ONLY dev-one at t+10s.
        self.clock.advance(10.0)
        reconcile_active_sessions(
            self.state_dir,
            bridge_url=self.mock_url,
            snapshots=SnapshotBundle(
                remote_snapshots=((self.dev_one, None), (self.dev_two, snap_two)),
                remotes_configured=True,
                enabled_remote_hosts=("dev-one", "dev-two"),
                remotes_checked=True,
                fetched_at_wall=self.clock.time(),
                fetched_at_ns=self.clock.time_ns(),
            ),
        )
        self.assertEqual(self.bridge.posts(), [])

        # Simulate the 300.001s scheduled wake pass (0.5s after a 300.0s poll) where collect_snapshots
        # is throttled and passes remote_snapshots=() — expire_unreachable_remotes MUST still fire!
        self.clock.advance(REMOTE_UNREACHABLE_GRACE_SECONDS + 0.5)
        reconcile_active_sessions(
            self.state_dir,
            bridge_url=self.mock_url,
            snapshots=SnapshotBundle(
                remote_snapshots=(),
                remotes_configured=True,
                enabled_remote_hosts=("dev-one", "dev-two"),
                remotes_checked=True,
                fetched_at_wall=self.clock.time(),
                fetched_at_ns=self.clock.time_ns(),
            ),
        )
        posts = self.bridge.posts()
        self.assertEqual([p["session_id"] for p in posts], ["herdr:dev-one:w1:p1"])
        self.assertEqual(posts[0]["state"], "Ended")
        mid_cache = read_cache(self.cache_mgr)
        self.assertNotIn("dev-one", mid_cache["remote_unreachable_since"])
        self.assertEqual(set(mid_cache["sessions"]), {"herdr:dev-two:w2:p1"})

        # Now disable/remove dev-two in endpoints.json (enabled_remote_hosts=(), remotes_checked=True).
        self.bridge.requests.clear()
        self.clock.advance(3.0)
        disabled_t0 = self.clock.time()
        reconcile_active_sessions(
            self.state_dir,
            bridge_url=self.mock_url,
            snapshots=SnapshotBundle(
                remote_snapshots=(),
                remotes_configured=False,
                enabled_remote_hosts=(),
                remotes_checked=True,
                fetched_at_wall=disabled_t0,
                fetched_at_ns=self.clock.time_ns(),
            ),
        )
        self.assertEqual(self.bridge.posts(), [])
        self.assertEqual(read_cache(self.cache_mgr)["remote_unreachable_since"].get("dev-two"), disabled_t0)

        self.clock.advance(REMOTE_UNREACHABLE_GRACE_SECONDS + 1.0)
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        end_posts = self.bridge.posts()
        self.assertEqual([p["session_id"] for p in end_posts], ["herdr:dev-two:w2:p1"])
        self.assertEqual(end_posts[0]["state"], "Ended")
        after_all = read_cache(self.cache_mgr)
        self.assertEqual(after_all["sessions"], {})
        self.assertEqual(after_all["remote_unreachable_since"], {})

    def test_remote_local_hostname_collision_disambiguates_and_preserves_local_isolation(self) -> None:
        handle_agent_status_changed(
            {
                "pane_id": "w1:p1",
                "workspace_id": "w1",
                "agent": "claude",
                "agent_status": "working",
                "title": "Local Mac session",
            },
            {},
            bridge_url=self.mock_url,
        )
        local_sid = self.sid("w1:p1")
        local_marker = pane_file(self.state_dir, "w1:p1")
        self.assertTrue(local_marker.exists())
        self.bridge.requests.clear()

        # Pass a RemoteEndpoint whose host collides directly with self.host!
        colliding_ep = RemoteEndpoint(
            id="ep-collide",
            host=self.host,
            machine_arg="10.0.0.55",
            label=self.host,
        )
        expected_remote_host = disambiguate_remote_host(self.host, "ep-collide", self.host)
        self.assertNotEqual(expected_remote_host, self.host)
        remote_sid = f"herdr:{expected_remote_host}:w1:p1"

        self.clock.advance(3.0)
        reconcile_active_sessions(
            self.state_dir,
            bridge_url=self.mock_url,
            snapshots=SnapshotBundle(
                remote_snapshots=(
                    (
                        colliding_ep,
                        {"panes": [{"pane_id": "w1:p1", "workspace_id": "w1", "agent": "agy", "agent_status": "working"}]},
                    ),
                ),
                remotes_configured=True,
                enabled_remote_hosts=(expected_remote_host,),
                remotes_checked=True,
                fetched_at_wall=self.clock.time(),
                fetched_at_ns=self.clock.time_ns(),
            ),
        )
        posts = self.bridge.posts()
        self.assertEqual(len(posts), 1, posts)
        self.assertEqual(posts[0]["session_id"], remote_sid)
        self.assertEqual(posts[0]["agent"], f"Antigravity (Herdr \u00b7 {expected_remote_host})")

        # Close remote w1:p1 -> local marker and local w1:p1 session must remain 100% untouched!
        self.bridge.requests.clear()
        self.clock.advance(3.0)
        reconcile_active_sessions(
            self.state_dir,
            bridge_url=self.mock_url,
            snapshots=SnapshotBundle(
                remote_snapshots=((colliding_ep, {"panes": []}),),
                remotes_configured=True,
                enabled_remote_hosts=(expected_remote_host,),
                remotes_checked=True,
                fetched_at_wall=self.clock.time(),
                fetched_at_ns=self.clock.time_ns(),
            ),
        )
        close_posts = self.bridge.posts()
        self.assertEqual([p["session_id"] for p in close_posts], [remote_sid])
        self.assertEqual(close_posts[0]["state"], "Ended")
        self.assertTrue(local_marker.exists())
        self.assertEqual(set(read_cache(self.cache_mgr)["sessions"]), {local_sid})

    def test_cap_eviction_orphan_exports_durability_and_rollback_on_journal_failure(self) -> None:
        now = self.clock.time()
        seeded = {
            self.sid(f"w1:pLive{i}"): session(f"w1:pLive{i}", "Working", now=now + i * 0.01)
            for i in range(SESSION_CAP - 1)
        }
        ended_sid = self.sid("w1:pOwedEnded")
        seeded[ended_sid] = session(
            "w1:pOwedEnded",
            "Ended",
            seq=2,
            delivered=False,
            now=now - 100.0,
            delivery_status="retryable_exhausted",
        )
        self.assertEqual(len(seeded), SESSION_CAP)
        seed(self.cache_mgr, seeded)

        new_remote_snap = SnapshotBundle(
            remote_snapshots=(
                (
                    self.dev_one,
                    {"panes": [{"pane_id": "w9:p257", "workspace_id": "w9", "agent": "agy", "agent_status": "working"}]},
                ),
            ),
            remotes_configured=True,
            enabled_remote_hosts=("dev-one",),
            remotes_checked=True,
            fetched_at_wall=now + 10.0,
            fetched_at_ns=int((now + 10.0) * 1e9),
        )

        # 1. Journal failure raises OrphanMirrorError BEFORE cache_mgr.save: no data loss!
        with mock.patch("herdr_bartender.sender.step_a.journal_orphan_exports", side_effect=OSError("disk full")):
            with self.assertRaises(OrphanMirrorError):
                reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url, snapshots=new_remote_snap)
        self.assertEqual(self.bridge.posts(), [])
        intact_sessions = read_cache(self.cache_mgr)["sessions"]
        self.assertIn(ended_sid, intact_sessions, "undelivered Ended stays intact on disk when journaling fails")
        self.assertNotIn("herdr:dev-one:w9:p257", intact_sessions)

        # 2. Normal run journals before save, folds into orphan file outside lock, and delivers the 257th session!
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url, snapshots=new_remote_snap)
        orphans = read_orphan_records(get_orphan_path())
        self.assertIn(ended_sid, orphans)
        self.assertEqual(orphans[ended_sid]["desired_state"], "Ended")
        self.assertIs(orphans[ended_sid]["orphaned_ended"], True)
        self.assertEqual([p["session_id"] for p in self.bridge.posts()], ["herdr:dev-one:w9:p257"])

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
