"""Pure Step A close staging: tombstone discipline and stable close origins (Plan §4.1, §4.3 Step A, item 5)."""

import subprocess
import time
import unittest

import copy

from herdr_bartender import cache
from herdr_bartender.cache import prepare_for_save
from herdr_bartender.cache_fields import sane_cache_fields
from herdr_bartender.cache_schema import new_cache, normalize_cache
from herdr_bartender.delivery_state import Outcome, Transmission, apply_delivery_result, stage_compensation
from herdr_bartender.handlers import (
    handle_agent_status_changed,
    handle_pane_closed,
    handle_tab_closed,
    handle_workspace_closed,
)
from herdr_bartender.intake import resolve_identity
from herdr_bartender.markers import touch_pane_marker
from herdr_bartender.multihost import host_matches_session, is_local_host, pane_scope_key, resolve_event_host
from herdr_bartender.orphans import export_orphan_record, orphan_pane_ids
from herdr_bartender.paths import get_orphan_path
from herdr_bartender.process import get_process_start_time
from herdr_bartender.replay import run_replay_orphans
from herdr_bartender.sanitize import AGENT_MAX, format_agent_name, get_hex_pane_id
from herdr_bartender.staging import SESSION_CAP, stage_pane_close_result, stage_status
from tests.support import SandboxTestCase


class CloseStagingTests(SandboxTestCase):
    def _working(self, pane, arr_ns, tab="w1:t1"):
        handle_agent_status_changed({"agent_status": "working", "pane_id": pane, "workspace_id": pane.split(":")[0],
                                     "agent": "claude", "tab_id": tab}, {}, bridge_url=self.mock_url, arrival_ns=arr_ns)

    def _data(self):
        with self.cache_mgr as data:
            return data

    def test_stale_close_does_not_tombstone_readmitted_pane(self):
        """Gap tombstone-on-stale-close: a close predating the session's admission changes nothing."""
        t0 = time.time_ns()
        self._working("w1:pStale", t0)
        handle_pane_closed({"pane_id": "w1:pStale"}, {}, bridge_url=self.mock_url, arrival_ns=t0 - 1_000)
        data = self._data()
        self.assertNotIn("w1:pStale", data["tombstones"])
        self.assertEqual(data["sessions"][self.sid("w1:pStale")]["desired_state"], "Working")

    def test_close_without_sessions_still_persists_tombstone(self):
        """Gap tombstone-on-stale-close: the tombstone is saved even when no session is cached."""
        handle_pane_closed({"pane_id": "w1:pNoSession"}, {}, bridge_url=self.mock_url, arrival_ns=time.time_ns())
        self.assertIn("w1:pNoSession", self._data()["tombstones"])

    def test_duplicate_close_keeps_original_origin(self):
        """Plan §1 L86 / gap closed-source-ts-not-persisted: tab.closed then pane.closed (dual teardown) keeps the
        first closed_at_ns and closed_source_ts on the session and the tombstone."""
        t0 = time.time_ns()
        self._working("w1:pDual", t0)
        self.bridge.return_code = 500  # keep the session cached across both closes
        handle_tab_closed({"tab_id": "w1:t1"}, {}, bridge_url=self.mock_url, arrival_ns=t0 + 1_000)
        first = dict(self._data()["sessions"][self.sid("w1:pDual")])
        handle_pane_closed({"pane_id": "w1:pDual"}, {}, bridge_url=self.mock_url, arrival_ns=t0 + 9_000)
        data = self._data()
        session = data["sessions"][self.sid("w1:pDual")]
        self.assertEqual((session["closed_at_ns"], session["closed_source_ts"]),
                         (first["closed_at_ns"], first["closed_source_ts"]))
        self.assertEqual(data["tombstones"]["w1:pDual"]["closed_at_ns"], t0 + 1_000)
        self.assertGreater(session["seq"], first["seq"], "the duplicate close still re-sends Ended")

    def test_tombstone_source_ts_survives_step_c(self):
        """Gap closed-source-ts-not-persisted: Step C re-records the tombstone from the persisted closed_source_ts."""
        t0 = time.time_ns()
        self._working("w1:pSrc", t0)
        handle_pane_closed({"pane_id": "w1:pSrc", "timestamp": 12345.5}, {}, bridge_url=self.mock_url,
                           arrival_ns=t0 + 1_000)
        data = self._data()
        self.assertNotIn(self.sid("w1:pSrc"), data["sessions"])
        self.assertEqual(data["tombstones"]["w1:pSrc"]["closed_source_ts"], max(12345.5, (t0 + 1_000) / 1e9))

    def test_cascade_tombstones_panes_leased_elsewhere(self):
        """Gap cascade-tombstone-only-when-leased: a session leased by a live foreign sender is tombstoned too,
        so a later pre-close status cannot resurrect it."""
        t0 = time.time_ns()
        self._working("w1:pLeased", t0)
        holder = subprocess.Popen(["sleep", "30"])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        with self.cache_mgr as data:
            data["sessions"][self.sid("w1:pLeased")].update({
                "sending_pid": holder.pid, "lease_deadline": time.time() + 30,
                "lease_token": f"{holder.pid}:{get_process_start_time(holder.pid)}:0:x"})
            self.cache_mgr.save(data)
        posts = len(self.bridge.requests)
        handle_tab_closed({"tab_id": "w1:t1"}, {}, bridge_url=self.mock_url, arrival_ns=t0 + 1_000)
        self.assertEqual(len(self.bridge.requests), posts, "leased elsewhere: deferred, not sent")
        self.assertIn("w1:pLeased", self._data()["tombstones"])
        handle_agent_status_changed({"agent_status": "idle", "pane_id": "w1:pLeased", "workspace_id": "w1",
                                     "agent": "claude"}, {}, bridge_url=self.mock_url, arrival_ns=t0 + 2_000)
        self.assertEqual(self._data()["sessions"][self.sid("w1:pLeased")]["desired_state"], "Ended")

    def test_cascade_overflow_single_lock_and_unleased(self):
        """Plan §5.2 (gaps cascade-overflow-two-locks, cascade-reconciler-spawn-in-lock): one session inline; the rest
        are staged in_flight and unleased in the SAME Step A lock hold; the reconciler is flagged after unlock."""
        t0 = time.time_ns()
        for i in range(3):
            self._working(f"w1:pOver{i}", t0 + i)
        posts = len(self.bridge.requests)
        entered = []
        real_enter = cache.BoundedSessionCache.__enter__

        def counting_enter(mgr):
            entered.append(1)
            return real_enter(mgr)

        cache.BoundedSessionCache.__enter__ = counting_enter
        try:
            handle_tab_closed({"tab_id": "w1:t1"}, {}, bridge_url=self.mock_url, arrival_ns=t0 + 1_000)
        finally:
            cache.BoundedSessionCache.__enter__ = real_enter
        self.assertEqual(len(self.bridge.requests) - posts, 1, "exactly one inline Ended")
        self.assertEqual(len(entered), 2, "Step A + Step C only (no second Step A lock for overflow)")
        data = self._data()
        overflow = [s for s in data["sessions"].values() if s["desired_state"] == "Ended"]
        self.assertEqual(len(overflow), 2)
        for s in overflow:
            self.assertEqual((s["lease_token"] if "lease_token" in s else None, s["delivery_status"]), (None, "in_flight"))
        self.assertTrue((self.state_dir / "reconciler.pending").exists())


class CapacityStagingTests(SandboxTestCase):
    """Plan §4.3 Step A "Strict 256 Active Session Capacity Check" (gap capacity-prune-before-reject)."""

    start_bridge = False
    NEW_PANE = "wCap:pNew"

    def _full_cache(self, **extra):
        live = {f"herdr:{self.host}:wCap:p{i}": {"desired_state": "Working", "seq": 1, "pane_id": f"wCap:p{i}",
                                                  "last_event_at": 2000.0 + i}
                for i in range(SESSION_CAP - len(extra))}
        data = new_cache(self.host)
        data["sessions"] = {**live, **{self.sid(pane): rec for pane, rec in extra.items()}}
        self.assertEqual(len(data["sessions"]), SESSION_CAP)
        return data

    def _stage(self, data, agent_status="working"):
        event = {"agent_status": agent_status, "pane_id": self.NEW_PANE, "workspace_id": "wCap", "agent": "claude"}
        identity, _ = resolve_identity(event, {}, env={})
        return stage_status(data, identity, event, {}, time.time_ns(), herdr_alive=lambda: True)

    def test_new_pane_at_cap_prunes_salvaged_record_first(self):
        """255 live + 1 salvaged: the salvaged record is pruned and session 257 is admitted (cache stays at 256)."""
        data = self._full_cache(**{"wOld:pSalvaged": {"desired_state": "Idle", "seq": 1, "delivered_seq": 1,
                                                      "salvaged": True, "pane_id": "wOld:pSalvaged",
                                                      "last_event_at": 1.0}})
        stage = self._stage(data)
        self.assertTrue(stage.staged, stage.reason)
        self.assertIn(self.sid(self.NEW_PANE), data["sessions"])
        self.assertNotIn(self.sid("wOld:pSalvaged"), data["sessions"])
        self.assertEqual(len(data["sessions"]), SESSION_CAP)

    def test_new_pane_at_cap_prunes_confirmed_ended_record(self):
        """255 live + 1 Ended already delivered: the Ended record is pruned and session 257 is admitted."""
        data = self._full_cache(**{"wOld:pDone": {"desired_state": "Ended", "delivered_state": "Ended", "seq": 3,
                                                  "delivered_seq": 3, "pane_id": "wOld:pDone",
                                                  "last_event_at": 1.0}})
        self.assertTrue(self._stage(data).staged)
        self.assertNotIn(self.sid("wOld:pDone"), data["sessions"])
        self.assertEqual(len(data["sessions"]), SESSION_CAP)

    def test_new_pane_at_cap_never_evicts_live_or_undelivered_sessions(self):
        """256 live, or 255 live + 1 Ended still owed to Bartender (not orphaned): admission is refused and the
        cache is untouched (an undelivered Ended is never dropped to make room)."""
        cases = {
            "all live": self._full_cache(),
            "undelivered Ended": self._full_cache(**{"wOld:pOwed": {
                "desired_state": "Ended", "seq": 2, "delivered_seq": 1, "delivery_status": "in_flight",
                "pane_id": "wOld:pOwed", "last_event_at": 1.0}}),
        }
        for name, data in cases.items():
            with self.subTest(case=name):
                before = copy.deepcopy(data)
                stage = self._stage(data)
                self.assertFalse(stage.staged)
                self.assertIn("capacity", stage.reason)
                self.assertEqual(data, before)

    def test_dropped_event_at_cap_prunes_nothing(self):
        """Step A decides before it mutates: an event dropped after the capacity check (transient unknown status)
        leaves the evictable record in place."""
        data = self._full_cache(**{"wOld:pSalvaged": {"desired_state": "Idle", "seq": 1, "delivered_seq": 1,
                                                      "salvaged": True, "pane_id": "wOld:pSalvaged",
                                                      "last_event_at": 1.0}})
        before = copy.deepcopy(data)
        self.assertFalse(self._stage(data, agent_status="unknown").staged)
        self.assertEqual(data, before)


class AgentNameAndSchemaMultiHostTests(SandboxTestCase):
    start_bridge = False   # normalize_cache logs dropped fields to plugin.log; keep it sandboxed
    def test_format_agent_name_local_remote_and_display_agent(self):
        self.assertEqual(format_agent_name("claude"), "Claude (Herdr)")
        self.assertEqual(format_agent_name(""), "Herdr")
        self.assertEqual(format_agent_name(None), "Herdr")
        self.assertEqual(format_agent_name("custom"), "Custom (Herdr)")
        self.assertEqual(format_agent_name("claude", display_agent="Ignored Override"), "Claude (Herdr)")
        self.assertEqual(format_agent_name("custom_bot", display_agent="Custom Bot"), "Custom Bot (Herdr)")
        self.assertEqual(format_agent_name("", display_agent="Custom Bot"), "Custom Bot (Herdr)")
        self.assertEqual(format_agent_name("agy", remote_host="dev-one"), "Antigravity (Herdr \u00b7 dev-one)")
        self.assertEqual(format_agent_name("custom_bot", remote_host="DEV-ONE.corp.local",
                                           display_agent="Custom Bot"),
                         "Custom Bot (Herdr \u00b7 dev-one)")
        self.assertEqual(format_agent_name("", remote_host="dev-one"), "Herdr (dev-one)")
        self.assertEqual(format_agent_name("", remote_host="   "), "Herdr")
        long_formatted = format_agent_name("x" * 100, remote_host="h" * 50, display_agent="D" * 100)
        self.assertEqual(len(long_formatted), AGENT_MAX)
        self.assertTrue(long_formatted.endswith(f" (Herdr \u00b7 {'h' * 32})"))

    def test_multihost_pure_helpers(self):
        self.assertEqual(resolve_event_host({}, "macbook"), ("macbook", False))
        self.assertEqual(resolve_event_host({"host": "   "}, "macbook"), ("macbook", False))
        self.assertEqual(resolve_event_host({"host": "MACBOOK.local"}, "macbook"), ("macbook", False))
        self.assertEqual(resolve_event_host({"host": "Dev-One.example.com"}, "macbook"), ("dev-one", True))
        self.assertEqual(pane_scope_key("w1:p1", "", "macbook"), "w1:p1")
        self.assertEqual(pane_scope_key("w1:p1", "macbook", "macbook"), "w1:p1")
        self.assertEqual(pane_scope_key("w1:p1", "dev-one", "macbook"), "dev-one@w1:p1")
        self.assertTrue(is_local_host(None, "macbook"))
        self.assertTrue(is_local_host("", "macbook"))
        self.assertTrue(is_local_host("macbook", "macbook"))
        self.assertFalse(is_local_host("dev-one", "macbook"))
        self.assertTrue(host_matches_session({}, "macbook", "macbook"))
        self.assertFalse(host_matches_session({}, "dev-one", "macbook"))
        self.assertTrue(host_matches_session({"host": "dev-one"}, "dev-one", "macbook"))
        self.assertFalse(host_matches_session({"host": "dev-one"}, "macbook", "macbook"))

    def test_remote_unreachable_since_schema_and_field_sanity(self):
        fresh = new_cache("macbook")
        self.assertEqual(fresh["remote_unreachable_since"], {})
        notes_seen = []
        raw = {
            "sessions": {},
            "remote_unreachable_since": {
                "dev-one": 100.5,
                "dev-two": "200.25",
                "": 50.0,
                "bad-bool": True,
                "bad-nan": "NaN",
                "bad-inf": float("inf"),
                "bad-str": "never",
                "bad-none": None,
            },
        }
        normalized = normalize_cache(raw, lambda: "macbook")
        self.assertEqual(normalized["remote_unreachable_since"], {"dev-one": 100.5, "dev-two": 200.25})
        sane_cache_fields({"sessions": {}, "remote_unreachable_since": {"": 1.0, "bad": "nope", "ok": 3}},
                          warn=notes_seen.append)
        self.assertEqual(len(notes_seen), 1)
        self.assertIn("remote_unreachable_since ''", notes_seen[0])
        self.assertIn("remote_unreachable_since bad", notes_seen[0])


class MultiHostAndNewTurnStagingTests(SandboxTestCase):
    def _data(self):
        with self.cache_mgr as data:
            return data

    def test_same_pane_id_isolated_across_local_and_remote_hosts(self):
        t0 = time.time_ns()
        pane = "w1:pShared"
        local_sid = self.sid(pane)
        remote_sid = f"herdr:dev-one:{pane}"
        marker = self.state_dir / "panes" / get_hex_pane_id(pane)

        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": pane, "workspace_id": "w1", "agent": "claude",
             "tab_id": "w1:t1", "title": "Local work"},
            {}, bridge_url=self.mock_url, arrival_ns=t0,
        )
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": pane, "workspace_id": "w1", "agent": "agy",
             "display_agent": "Custom Agy", "host": "dev-one", "tab_id": "w1:t1", "title": "Remote work"},
            {}, bridge_url=self.mock_url, arrival_ns=t0 + 10,
        )

        data = self._data()
        self.assertIn(local_sid, data["sessions"])
        self.assertIn(remote_sid, data["sessions"])
        self.assertEqual(data["sessions"][local_sid]["host"], self.host)
        self.assertEqual(data["sessions"][local_sid]["agent"], "Claude (Herdr)")
        self.assertEqual(data["sessions"][remote_sid]["pane_id"], pane)
        self.assertEqual(data["sessions"][remote_sid]["host"], "dev-one")
        self.assertEqual(data["sessions"][remote_sid]["agent"], "Antigravity (Herdr \u00b7 dev-one)")
        self.assertIn(pane, data["pane_generations"])
        self.assertIn(f"dev-one@{pane}", data["pane_generations"])
        self.assertTrue(marker.exists(), "local session created local pane marker")

        # Remote pane.closed only closes remote session, tombstones dev-one@w1:pShared, keeps local marker & vendor free.
        handle_pane_closed({"pane_id": pane, "workspace_id": "w1", "host": "dev-one"}, {},
                           bridge_url=self.mock_url, arrival_ns=t0 + 100)
        data = self._data()
        self.assertIn(local_sid, data["sessions"])
        self.assertNotIn(remote_sid, data["sessions"])
        self.assertIn(f"dev-one@{pane}", data["tombstones"])
        self.assertNotIn(pane, data["tombstones"])
        self.assertTrue(marker.exists(), "remote pane close must NOT remove local pane marker")

        # Local non-working status still succeeds (not blocked by remote tombstone).
        handle_agent_status_changed(
            {"agent_status": "blocked", "pane_id": pane, "workspace_id": "w1", "agent": "claude"},
            {}, bridge_url=self.mock_url, arrival_ns=t0 + 200,
        )
        self.assertEqual(self._data()["sessions"][local_sid]["desired_state"], "Waiting")

    def test_remote_container_close_and_agent_exit_do_not_affect_local_sessions(self):
        t0 = time.time_ns()
        pane = "w1:pTabIso"
        local_sid = self.sid(pane)
        remote_sid = f"herdr:dev-one:{pane}"

        for extra in ({}, {"host": "dev-one"}):
            handle_agent_status_changed(
                {"agent_status": "working", "pane_id": pane, "workspace_id": "w1", "agent": "codex",
                 "tab_id": "w1:tShared", **extra},
                {}, bridge_url=self.mock_url, arrival_ns=t0,
            )

        # Remote agent exit records dev-one@pane in agent_exits, leaving local session live.
        handle_agent_status_changed(
            {"agent_status": "idle", "pane_id": pane, "workspace_id": "w1", "agent": "",
             "host": "dev-one", "timestamp": 500.0},
            {}, bridge_url=self.mock_url, arrival_ns=t0 + 50,
        )
        data = self._data()
        self.assertIn(local_sid, data["sessions"])
        self.assertNotIn(remote_sid, data["sessions"])
        self.assertIn(f"dev-one@{pane}", data["agent_exits"])
        self.assertNotIn(pane, data["agent_exits"])

        # Re-admit remote session, then close remote tab & workspace; local session stays Working.
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": pane, "workspace_id": "w1", "agent": "codex",
             "tab_id": "w1:tShared", "host": "dev-one"},
            {}, bridge_url=self.mock_url, arrival_ns=t0 + 100,
        )
        handle_tab_closed({"tab_id": "w1:tShared", "workspace_id": "w1", "host": "dev-one"}, {},
                          bridge_url=self.mock_url, arrival_ns=t0 + 200)
        data = self._data()
        self.assertEqual(data["sessions"][local_sid]["desired_state"], "Working")
        self.assertNotIn(remote_sid, data["sessions"])
        self.assertIn(f"dev-one@{pane}", data["tombstones"])
        self.assertNotIn(pane, data["tombstones"])

        # Re-admit remote in another pane, then close local workspace -> remote survives.
        remote_pane2 = "w1:pRem2"
        remote_sid2 = f"herdr:dev-one:{remote_pane2}"
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": remote_pane2, "workspace_id": "w1", "agent": "codex",
             "host": "dev-one"},
            {}, bridge_url=self.mock_url, arrival_ns=t0 + 300,
        )
        handle_workspace_closed({"workspace_id": "w1"}, {}, bridge_url=self.mock_url, arrival_ns=t0 + 400)
        data = self._data()
        self.assertNotIn(local_sid, data["sessions"])
        self.assertIn(remote_sid2, data["sessions"])
        self.assertEqual(data["sessions"][remote_sid2]["desired_state"], "Working")

    def test_remote_step_c_never_touches_local_markers_or_vendor_cleanups(self):
        pane = "w1:pRemoteStepC"
        remote_sid = f"herdr:dev-one:{pane}"
        hex_id = get_hex_pane_id(pane)
        panes_dir = self.state_dir / "panes"
        data = new_cache(self.host)
        data["tombstones"][pane] = {"closed_at_ns": 999999, "closed_source_ts": 99.0, "last_source_timestamp": 99.0}
        data["sessions"][remote_sid] = {
            "pane_id": pane, "host": "dev-one", "agent": "Claude (Herdr \u00b7 dev-one)",
            "desired_state": "Working", "seq": 1, "delivered_seq": 0, "generation": 2, "admitted_at_ns": 100,
            "lease_token": "tok-r", "sending_pid": 1, "lease_deadline": time.time() + 1.5,
            "delivery_status": "in_flight", "delivery_attempts": 0, "resync_generation": 0,
        }
        tx = Transmission(remote_sid, pane, "Working", 1, "Claude (Herdr \u00b7 dev-one)", "tok-r", 0, 2, 100, 200)

        # Local tombstone on bare `pane` does NOT tombstone the remote delivery; success writes no local marker/cleanup.
        eff = apply_delivery_result(data, tx, Outcome("success"))
        self.assertEqual(eff.verdict, "delivered")
        self.assertEqual(eff.vendor_cleanups, ())
        self.assertEqual(data["pending_vendor_cleanups"], [])
        self.assertFalse((panes_dir / hex_id).exists())

        # Retryable and non_retryable failures on remote session do NOT create local .failed or remove local marker.
        touch_pane_marker(pane)
        self.assertTrue((panes_dir / hex_id).exists())
        data["sessions"][remote_sid].update({"seq": 2, "lease_token": "tok-r2"})
        tx2 = Transmission(remote_sid, pane, "Waiting", 2, "Claude (Herdr \u00b7 dev-one)", "tok-r2", 0, 2, 100, 300)
        apply_delivery_result(data, tx2, Outcome("retryable", "network_timeout"))
        self.assertTrue((panes_dir / hex_id).exists())
        self.assertFalse((panes_dir / f"{hex_id}.failed").exists())

        data["sessions"][remote_sid].update({"lease_token": "tok-r3"})
        tx3 = Transmission(remote_sid, pane, "Waiting", 2, "Claude (Herdr \u00b7 dev-one)", "tok-r3", 0, 2, 100, 300)
        apply_delivery_result(data, tx3, Outcome("non_retryable", "4xx_client_error"))
        self.assertTrue((panes_dir / hex_id).exists())
        self.assertFalse((panes_dir / f"{hex_id}.failed").exists())

        # Remote Ended eviction keeps local marker and records tombstone under dev-one@pane.
        data["sessions"][remote_sid].update({
            "desired_state": "Ended", "seq": 3, "lease_token": "tok-r4",
            "close_kind": "container", "closed_at_ns": 555, "closed_source_ts": 12.0,
        })
        tx4 = Transmission(remote_sid, pane, "Ended", 3, "Claude (Herdr \u00b7 dev-one)", "tok-r4", 0, 2, 100, 400)
        ev = apply_delivery_result(data, tx4, Outcome("success"))
        self.assertTrue(ev.evicted)
        self.assertTrue((panes_dir / hex_id).exists(), "local marker preserved across remote eviction")
        self.assertIn(f"dev-one@{pane}", data["tombstones"])
        self.assertEqual(data["pending_vendor_cleanups"], [])

        # stage_pane_close_result for remote pane returns recorded=False and adds no vendor cleanup.
        closed = stage_pane_close_result(data, pane, {"pane_id": pane, "host": "dev-two"}, 777)
        self.assertFalse(closed.recorded)
        self.assertIn(f"dev-two@{pane}", data["tombstones"])
        self.assertEqual(data["pending_vendor_cleanups"], [])

    def test_pane_generations_save_prune_handles_remote_scoped_keys(self):
        export_orphan_record("herdr:dev-two:w9:pOrphan",
                             {"pane_id": "w9:pOrphan", "host": "dev-two", "desired_state": "Ended"},
                             blocking=True)
        scoped_orphans = orphan_pane_ids()
        self.assertIn("dev-two@w9:pOrphan", scoped_orphans)

        with self.cache_mgr as data:
            data["sessions"]["herdr:dev-one:w9:p1"] = {
                "pane_id": "w9:p1", "host": "dev-one", "desired_state": "Working", "seq": 1,
            }
            data["pane_generations"] = {
                "dev-one@w9:p1": 1,       # active remote session: must stay protected despite lowest gen
                "w9:p1": 2,               # unprotected local key on same pane_id: must be pruned
                "dev-two@w9:pOrphan": 3,  # remote orphan: must stay protected via orphan_pane_ids()
                **{f"dev-one@w9:pFill{i}": i + 10 for i in range(520)},
            }
            prepare_for_save(data, time.time(), time.time_ns(), orphan_panes=frozenset())
            self.assertEqual(len(data["pane_generations"]), 512)
            self.assertIn("dev-one@w9:p1", data["pane_generations"],
                          "active remote session protects its <host>@<pane> generation key")
            self.assertNotIn("w9:p1", data["pane_generations"],
                             "unprotected local pane key with same pane_id is pruned, not falsely protected")

            # Also verify orphan_pane_ids() protects remote orphan scope keys on save.
            data["pane_generations"]["dev-two@w9:pOrphan"] = 3
            data["pane_generations"]["w9:pOrphan"] = 4
            data["pane_generations"]["dev-one@w9:pExtra"] = 9999
            self.cache_mgr.save(data, orphan_panes=scoped_orphans)
            self.assertEqual(len(data["pane_generations"]), 512)
            self.assertIn("dev-one@w9:p1", data["pane_generations"])
            self.assertIn("dev-two@w9:pOrphan", data["pane_generations"])
            self.assertNotIn("w9:pOrphan", data["pane_generations"])

    def test_stage_compensation_and_orphan_replay_respect_host_isolation(self):
        # 1. stage_compensation never falls back to local pane_generations[pane] for a remote session.
        data = new_cache(self.host)
        data["pane_generations"]["w1:pComp"] = 77
        tx_untracked = Transmission("herdr:dev-one:w1:pComp", "w1:pComp", "Working", 1, generation=None)
        comp1 = stage_compensation(data, tx_untracked, 100.0)
        self.assertIsNone(comp1["generation"], "remote compensation must not read local host generation")
        data["pane_generations"]["dev-one@w1:pComp"] = 12
        comp2 = stage_compensation(data, tx_untracked, 101.0)
        self.assertEqual(comp2["generation"], 12)

        # 2. run_replay_orphans does not suppress or resync an active local session for a remote orphan,
        #    and never removes a local pane marker when replaying a remote orphan.
        pane = "w1:pReplayIso"
        local_sid = self.sid(pane)
        remote_sid = f"herdr:dev-one:{pane}"
        marker = self.state_dir / "panes" / get_hex_pane_id(pane)
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": pane, "workspace_id": "w1", "agent": "claude"},
            {}, bridge_url=self.mock_url,
        )
        self.assertTrue(marker.exists())
        export_orphan_record(remote_sid, {"pane_id": pane, "host": "dev-one", "agent": "Codex (Herdr \u00b7 dev-one)"},
                             blocking=True)
        self.assertTrue(run_replay_orphans(str(get_orphan_path()), bridge_url=self.mock_url, quiet=True))
        self.assertEqual([e["state"] for e in self.bridge.events_for(remote_sid)], ["Ended"],
                         "remote orphan must be replayed even when local host has active session on same pane_id")
        local_rec = self._data()["sessions"][local_sid]
        self.assertEqual((local_rec["delivery_status"], local_rec.get("resync_generation", 0)), ("delivered", 0),
                         "local session must not be forced to resync by remote orphan Ended")
        self.assertTrue(marker.exists())

        # Even with no cached local session, replaying a remote orphan leaves local pane marker intact.
        standalone_pane = "w1:pLocalMarkerOnly"
        touch_pane_marker(standalone_pane)
        standalone_marker = self.state_dir / "panes" / get_hex_pane_id(standalone_pane)
        export_orphan_record(f"herdr:dev-one:{standalone_pane}",
                             {"pane_id": standalone_pane, "host": "dev-one", "agent": "Herdr (dev-one)"},
                             blocking=True)
        self.assertTrue(run_replay_orphans(str(get_orphan_path()), bridge_url=self.mock_url, quiet=True))
        self.assertTrue(standalone_marker.exists(), "replaying remote orphan must never delete local pane marker")

    def test_new_turn_does_not_inherit_stale_fields_from_ended_turn(self):
        pane = "w1:pNewTurn"
        sid = self.sid(pane)
        t0 = time.time_ns()
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": pane, "workspace_id": "w1", "agent": "claude",
             "cwd": "/stale/cwd", "title": "Stale Turn Title", "tab_id": "w1:tStale"},
            {}, bridge_url=self.mock_url, arrival_ns=t0,
        )
        # Keep the Ended turn in cache by failing its bridge delivery.
        self.bridge.return_code = 500
        handle_agent_status_changed(
            {"agent_status": "idle", "pane_id": pane, "workspace_id": "w1", "agent": ""},
            {}, bridge_url=self.mock_url, arrival_ns=t0 + 100,
        )
        self.assertEqual(self._data()["sessions"][sid]["desired_state"], "Ended")

        # Start a new turn with no cwd/title/tab_id on the event: must NOT inherit stale Ended fields!
        self.bridge.return_code = 200
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": pane, "workspace_id": "w1", "agent": "codex"},
            {}, bridge_url=self.mock_url, arrival_ns=t0 + 200,
        )
        rec = self._data()["sessions"][sid]
        self.assertEqual(rec["desired_state"], "Working")
        self.assertEqual(rec["cwd"], "")
        self.assertEqual(rec["title"], f"Pane {pane}")
        self.assertIsNone(rec["tab_id"])


if __name__ == "__main__":
    unittest.main()
