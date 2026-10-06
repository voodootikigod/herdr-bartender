"""Event intake through the handlers: lifecycle, qualification, context isolation, close matching."""

import time
import unittest

from herdr_bartender.handlers import (
    handle_agent_status_changed,
    handle_pane_closed,
    handle_tab_closed,
    handle_workspace_closed,
)
from tests.support import SandboxTestCase


class StatusEventTests(SandboxTestCase):
    def _lifecycle_prefix(self):
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:p1", "workspace_id": "w1", "agent": "claude", "title": "Unit test turn"},
            {"tab_id": "w1:t1"},
            bridge_url=self.mock_url,
        )
        self.assertEqual(len(self.bridge.sessions), 1)
        self.assertEqual(list(self.bridge.sessions.values())[0]["state"], "Working")

        handle_agent_status_changed(
            {"agent_status": "blocked", "pane_id": "w1:p1", "workspace_id": "w1", "agent": "claude"},
            {"tab_id": "w1:t1"},
            bridge_url=self.mock_url,
        )
        self.assertEqual(list(self.bridge.sessions.values())[0]["state"], "Waiting")

    def test_p02_lifecycle_transitions(self):
        """Plan §10.1 #2 (gap t2-lifecycle): working -> blocked -> done -> idle on one pane; each state reaches the
        bridge in order and the cache tracks desired/delivered state with seq advancing by one."""
        self._lifecycle_prefix()
        for status in ("done", "idle"):
            handle_agent_status_changed({"agent_status": status, "pane_id": "w1:p1", "workspace_id": "w1",
                                         "agent": "claude"}, {"tab_id": "w1:t1"}, bridge_url=self.mock_url)
        sid = self.sid("w1:p1")
        sent = [(e["state"], e["seq"]) for e in self.bridge.events_for(sid)]
        self.assertEqual(sent, [("Working", 1), ("Waiting", 2), ("Done", 3), ("Idle", 4)])
        with self.cache_mgr as data:
            s = data["sessions"][sid]
        self.assertEqual((s["desired_state"], s["delivered_state"], s["seq"], s["delivered_seq"]), ("Idle", "Idle", 4, 4))
        self.assertEqual(self.bridge.sessions[sid]["state"], "Idle")

    def test_p05_unknown_status_debounced(self):
        """Plan §10.1 #5: an `unknown` status does not evict the active session (anti-flap)."""
        self._lifecycle_prefix()
        handle_agent_status_changed(
            {"agent_status": "unknown", "pane_id": "w1:p1", "workspace_id": "w1"},
            {"tab_id": "w1:t1"},
            bridge_url=self.mock_url,
        )
        self.assertEqual(len(self.bridge.sessions), 1)

    def test_p07_seq_strictly_monotonic(self):
        """Plan §10.1 #7 (gap t7-seq-monotonic): every accepted event advances seq by exactly one, dropped events
        do not, and the seq carried by the bridge payloads strictly increases."""
        t_base, seqs = time.time(), []
        events = [("working", 1), ("blocked", 2), ("working", 3), ("done", 0.5), ("idle", 4)]  # 0.5: stale, dropped
        for status, offset in events:
            handle_agent_status_changed({"agent_status": status, "pane_id": "w1:pMono", "workspace_id": "w1",
                                         "agent": "claude", "timestamp": t_base + offset}, {}, bridge_url=self.mock_url)
            with self.cache_mgr as data:
                seqs.append(data["sessions"][self.sid("w1:pMono")]["seq"])
        self.assertEqual(seqs, [1, 2, 3, 3, 4])
        sent = [e["seq"] for e in self.bridge.events_for(self.sid("w1:pMono"))]
        self.assertEqual(sent, [1, 2, 3, 4])

    def test_p07_out_of_order_source_timestamp(self):
        """Plan §10.1 #7: an older source timestamp is dropped and the integer seq is incremented."""
        t_base = time.time()
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pSeq", "workspace_id": "w1", "agent": "claude", "timestamp": t_base + 10},
            {},
            bridge_url=self.mock_url,
        )
        handle_agent_status_changed(
            {"agent_status": "idle", "pane_id": "w1:pSeq", "workspace_id": "w1", "agent": "claude", "timestamp": t_base + 2},
            {},
            bridge_url=self.mock_url,
        )
        with self.cache_mgr as data:
            sid = self.sid("w1:pSeq")
            self.assertEqual(data["sessions"][sid]["desired_state"], "Working", "Stale event must not overwrite state")
            self.assertGreaterEqual(data["sessions"][sid]["seq"], 1, "Integer seq must be incremented")

    def test_p18_background_pane_context_isolation(self):
        """Plan §10.1 #18: a background pane does not inherit the focused pane's cwd, tab_id or agent."""
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pBackground", "workspace_id": "w1", "agent": "claude"},
            {"focused_pane_id": "w1:pFocused", "focused_pane_cwd": "/secret/focused/dir", "focused_pane_agent": "codex",
             "workspace_cwd": "/workspace/default", "tab_id": "w1:tFocused"},
            bridge_url=self.mock_url,
        )
        with self.cache_mgr as data:
            s_bg = data["sessions"].get(self.sid("w1:pBackground"))
            self.assertIsNotNone(s_bg, "Background session must exist")
            self.assertEqual(s_bg.get("cwd"), "", f"Background pane must not inherit focused or workspace context cwd: {s_bg.get('cwd')}")
            self.assertIsNone(s_bg.get("tab_id"), f"Background pane must not inherit focused tab_id: {s_bg.get('tab_id')}")
            self.assertEqual(s_bg.get("agent"), "Claude (Herdr)", f"Background pane must not inherit focused pane agent: {s_bg.get('agent')}")

    def test_p18_background_focused_agent_never_admits(self):
        """Plan §10.1 #18 (gap tests-isolation-weak): a background event with no agent is not admitted via focused_pane_agent."""
        ctx = {"focused_pane_id": "w1:pFocused", "focused_pane_agent": "codex", "focused_pane_cwd": "/secret"}
        handle_agent_status_changed({"agent_status": "working", "pane_id": "w1:pBg2", "workspace_id": "w1"},
                                    ctx, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            self.assertNotIn(self.sid("w1:pBg2"), data.get("sessions", {}))
        self.assertEqual(self.bridge.history, [])

    def test_p18_background_session_keeps_own_agent(self):
        """Plan §10.1 #18 (gap tests-isolation-weak): an admitted background session never takes the focused agent."""
        handle_agent_status_changed({"agent_status": "working", "pane_id": "w1:pBg3", "workspace_id": "w1", "agent": "claude"},
                                    {}, bridge_url=self.mock_url)
        ctx = {"focused_pane_id": "w1:pFocused", "focused_pane_agent": "codex"}
        handle_agent_status_changed({"agent_status": "blocked", "pane_id": "w1:pBg3", "workspace_id": "w1"},
                                    ctx, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            s = data["sessions"][self.sid("w1:pBg3")]
            self.assertEqual((s["desired_state"], s["agent"]), ("Waiting", "Claude (Herdr)"))
        self.assertEqual(self.bridge.history[-1]["agent"], "Claude (Herdr)")

    def test_p18_status_without_pane_id_never_uses_focused_pane(self):
        """Plan §10.1 #18 / §2.3 (gap pane-closed-context-leak): a status event without pane_id is dropped, not applied to the focused pane."""
        handle_agent_status_changed({"agent_status": "working", "agent": "claude"},
                                    {"focused_pane_id": "w1:pFocused", "focused_pane_agent": "claude"}, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            self.assertEqual(data.get("sessions", {}), {})

    def test_p26_context_isolation_without_focused_pane(self):
        """Plan §10.1 #26: a None/missing focused_pane_id never leaks cwd or tab_id."""
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pNoneFocus", "workspace_id": "w1", "agent": "claude"},
            {"focused_pane_id": None, "focused_pane_cwd": "/secret/none/dir", "tab_id": "w1:tNone"},
            bridge_url=self.mock_url,
        )
        with self.cache_mgr as data:
            s_nf = data["sessions"].get(self.sid("w1:pNoneFocus"))
            self.assertIsNotNone(s_nf)
            self.assertEqual(s_nf.get("cwd"), "", "Missing/None focused_pane_id must not leak cwd")
            self.assertIsNone(s_nf.get("tab_id"), "Missing/None focused_pane_id must not leak tab_id")

    def test_p51_capacity_rejects_session_257(self):
        """Plan §10.1 #51: with 256 active sessions cached, session 257 is refused admission."""
        with self.cache_mgr as data:
            data["sessions"] = {
                f"herdr:{self.host}:wCap:p{i}": {
                    "desired_state": "Working",
                    "seq": 1,
                    "pane_id": f"wCap:p{i}",
                    "agent": "Claude (Herdr)",
                    "last_event_at": time.time(),
                }
                for i in range(256)
            }
            self.cache_mgr.save(data)

        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "wCap:p257", "workspace_id": "wCap", "agent": "claude"},
            {},
            bridge_url=self.mock_url,
        )
        with self.cache_mgr as data:
            self.assertNotIn(self.sid("wCap:p257"), data["sessions"], "Session 257 must be rejected when 256 active sessions exist")
            self.assertEqual(len(data["sessions"]), 256)

    def test_p51_capacity_prunes_salvaged_before_rejecting(self):
        """Plan §10.1 #51 / §4.3 capacity check (gap capacity-prune-before-reject): with 255 live sessions and one
        salvaged record cached, session 257 is admitted after pruning the salvaged record, and delivered."""
        live = {
            f"herdr:{self.host}:wCap:p{i}": {"desired_state": "Working", "seq": 1, "pane_id": f"wCap:p{i}",
                                              "agent": "Claude (Herdr)", "last_event_at": time.time()}
            for i in range(255)
        }
        salvaged_sid = self.sid("wOld:pSalvaged")
        with self.cache_mgr as data:
            data["sessions"] = {**live, salvaged_sid: {
                "desired_state": "Idle", "delivered_state": "Idle", "seq": 1, "delivered_seq": 1, "salvaged": True,
                "delivery_status": "salvaged", "pane_id": "wOld:pSalvaged", "last_event_at": time.time() - 60}}
            self.cache_mgr.save(data)

        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "wCap:p257", "workspace_id": "wCap", "agent": "claude"},
            {}, bridge_url=self.mock_url,
        )
        with self.cache_mgr as data:
            self.assertTrue(self.sid("wCap:p257") in data["sessions"], "session 257 admitted after pruning")
            self.assertFalse(salvaged_sid in data["sessions"], "the salvaged record made room")
            self.assertEqual(len(data["sessions"]), 256)
        self.assertEqual([e["state"] for e in self.bridge.events_for(self.sid("wCap:p257"))], ["Working"])

    def test_p59_agent_exit_without_container_tombstone(self):
        """Plan §10.1 #59: agent exit evicts without a container tombstone, records agent_exits, and a new agent re-admits."""
        pane_59 = "w1:pAgentExit"
        sid_59 = self.sid(pane_59)
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": pane_59, "workspace_id": "w1", "agent": "claude", "timestamp": 100.0},
            {},
            bridge_url=self.mock_url,
        )
        with self.cache_mgr as data:
            self.assertIn(sid_59, data.get("sessions", {}))
        handle_agent_status_changed(
            {"agent_status": "idle", "pane_id": pane_59, "workspace_id": "w1", "agent": "", "timestamp": 110.0},
            {},
            bridge_url=self.mock_url,
        )
        with self.cache_mgr as data:
            self.assertNotIn(sid_59, data.get("sessions", {}), "Agent session must be evicted on exit")
            self.assertNotIn(pane_59, data.get("tombstones", {}), "Agent exit from shell must NOT create a container tombstone")
            self.assertIn(pane_59, data.get("agent_exits", {}), "Agent exit must record in agent_exits table")
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": pane_59, "workspace_id": "w1", "agent": "claude", "timestamp": 105.0},
            {},
            bridge_url=self.mock_url,
        )
        with self.cache_mgr as data:
            self.assertNotIn(sid_59, data.get("sessions", {}), "Stale event predating agent exit must be dropped")
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": pane_59, "workspace_id": "w1", "agent": "codex"},
            {},
            bridge_url=self.mock_url,
        )
        with self.cache_mgr as data:
            self.assertIn(sid_59, data.get("sessions", {}), "New agent in open pane must be admitted even without source timestamp")
            self.assertNotIn(pane_59, data.get("agent_exits", {}), "Positive working admission must clear agent_exits entry")


    # -- §2.2 qualification ------------------------------------------------------
    def test_p04_unassisted_shell_ignored(self):
        """Plan §10.1 #4 (gap t4-qualification): a status for a pane with no agent and no session is ignored, no POST."""
        for status in ("working", "idle", "blocked", "done"):
            handle_agent_status_changed({"agent_status": status, "pane_id": "w1:pShell", "workspace_id": "w1"},
                                        {}, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            self.assertNotIn(self.sid("w1:pShell"), data.get("sessions", {}))
        self.assertEqual(self.bridge.requests, [])

    def test_p04_agent_exit_null_and_empty_end_session(self):
        """Plan §10.1 #4 / §2.2 Case B (gap t4-qualification): agent null and agent "" each POST Ended and evict."""
        for i, agent in enumerate((None, "")):
            pane = f"w1:pExit{i}"
            with self.subTest(agent=agent):
                handle_agent_status_changed({"agent_status": "working", "pane_id": pane, "workspace_id": "w1", "agent": "claude"},
                                            {}, bridge_url=self.mock_url)
                handle_agent_status_changed({"agent_status": "idle", "pane_id": pane, "workspace_id": "w1", "agent": agent},
                                            {}, bridge_url=self.mock_url)
                ended = [h for h in self.bridge.history if h.get("session_id") == self.sid(pane) and h.get("state") == "Ended"]
                self.assertEqual(len(ended), 1, "Ended must reach the bridge")
                with self.cache_mgr as data:
                    self.assertNotIn(self.sid(pane), data.get("sessions", {}), "Session must be evicted")

    def test_case_b_wins_over_focused_agent(self):
        """Plan §2.2 Case B (gap idle-case-c-omitted-key): agent null on a focused pane with stale focused agent -> Ended."""
        ctx = {"focused_pane_id": "w1:pFocB", "focused_pane_agent": "claude"}
        handle_agent_status_changed({"agent_status": "working", "pane_id": "w1:pFocB", "workspace_id": "w1", "agent": "claude"},
                                    ctx, bridge_url=self.mock_url)
        handle_agent_status_changed({"agent_status": "idle", "pane_id": "w1:pFocB", "workspace_id": "w1", "agent": None},
                                    ctx, bridge_url=self.mock_url)
        self.assertEqual(self.bridge.history[-1]["state"], "Ended")
        with self.cache_mgr as data:
            self.assertNotIn(self.sid("w1:pFocB"), data.get("sessions", {}))

    def test_case_c_omitted_agent_key_idles(self):
        """Plan §2.2 Case C (gap idle-case-c-omitted-key): idle with the agent key omitted -> Idle, agent retained."""
        handle_agent_status_changed({"agent_status": "working", "pane_id": "w1:pCaseC", "workspace_id": "w1", "agent": "codex"},
                                    {}, bridge_url=self.mock_url)
        handle_agent_status_changed({"agent_status": "idle", "pane_id": "w1:pCaseC", "workspace_id": "w1"},
                                    {}, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            s = data["sessions"][self.sid("w1:pCaseC")]
            self.assertEqual((s["desired_state"], s["agent"], s["raw_agent"]), ("Idle", "Codex (Herdr)", "codex"))
        self.assertEqual(self.bridge.history[-1]["state"], "Idle")

    def test_ended_session_not_resurrected_by_cached_agent(self):
        """Plan §2.2 (gap ended-resurrection-cached-agent): an Ended session still cached is not revived by 'working' without agent."""
        sid = self.sid("w1:pEnded")
        with self.cache_mgr as data:
            data.setdefault("sessions", {})[sid] = {
                "pane_id": "w1:pEnded", "workspace_id": "w1", "desired_state": "Ended", "raw_agent": "claude",
                "agent": "Claude (Herdr)", "seq": 3, "generation": 1, "delivery_status": "in_flight",
            }
            self.cache_mgr.save(data)
        handle_agent_status_changed({"agent_status": "working", "pane_id": "w1:pEnded", "workspace_id": "w1"},
                                    {}, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            self.assertEqual(data["sessions"][sid]["desired_state"], "Ended")
            self.assertEqual(data["sessions"][sid]["seq"], 3)
        self.assertEqual(self.bridge.history, [])

    def test_whitespace_agent_not_admitted(self):
        """Plan §2.2 L160 (gap admission-whitespace-agent): a whitespace-only agent is not a positive admission signal."""
        handle_agent_status_changed({"agent_status": "working", "pane_id": "w1:pWs", "workspace_id": "w1", "agent": "   "},
                                    {}, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            self.assertNotIn(self.sid("w1:pWs"), data.get("sessions", {}))

    def test_unrecognized_status_logs_and_leaves_cache(self):
        """Plan §2.1 (gap unrecognized-status-log): an unrecognized agent_status logs a warning and does not mutate the cache."""
        handle_agent_status_changed({"agent_status": "working", "pane_id": "w1:pUnrec", "workspace_id": "w1", "agent": "claude"},
                                    {}, bridge_url=self.mock_url)
        cache_file = self.state_dir / "active-sessions.json"
        before = cache_file.read_bytes()
        handle_agent_status_changed({"agent_status": "sleeping", "pane_id": "w1:pUnrec", "workspace_id": "w1", "agent": "claude"},
                                    {}, bridge_url=self.mock_url)
        self.assertEqual(cache_file.read_bytes(), before)
        log = (self.state_dir / "plugin.log").read_text()
        self.assertIn("Warning: unrecognized agent_status 'sleeping'", log)

    # -- §2.3 field resolution & sanitization -----------------------------------
    def test_fields_sanitized_end_to_end(self):
        """Plan §2.3 (gaps agent-sanitization, cwd-order-and-sanitization, title-csi-coverage): wire payload is sanitized."""
        handle_agent_status_changed({
            "agent_status": "working", "pane_id": "w1:pSan", "workspace_id": "w1",
            "agent": "\x1b]0;pwn\x07claude", "title": "\x1b[?25hHi\x1b[>4;2m!", "cwd": "/x\x00y" + "z" * 300,
        }, {}, bridge_url=self.mock_url)
        sent = self.bridge.history[-1]
        self.assertEqual(sent["agent"], "Claude (Herdr)")
        self.assertEqual(sent["title"], "Hi!")
        self.assertEqual(len(sent["cwd"]), 256)
        self.assertTrue(sent["cwd"].startswith("/xy"))

    def test_cached_cwd_beats_focused_cwd(self):
        """Plan §2.3 cwd row (gap cwd-order-and-sanitization): cached cwd precedes focused_pane_cwd."""
        handle_agent_status_changed({"agent_status": "working", "pane_id": "w1:pCwd", "workspace_id": "w1", "agent": "claude",
                                     "cwd": "/cached"}, {}, bridge_url=self.mock_url)
        handle_agent_status_changed({"agent_status": "blocked", "pane_id": "w1:pCwd", "workspace_id": "w1", "agent": "claude"},
                                    {"focused_pane_id": "w1:pCwd", "focused_pane_cwd": "/focused"}, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            self.assertEqual(data["sessions"][self.sid("w1:pCwd")]["cwd"], "/cached")

    def test_invalid_tab_id_not_stored(self):
        """Plan §2.3 tab_id row (gap tab-workspace-validation): an invalid tab_id is never stored."""
        handle_agent_status_changed({"agent_status": "working", "pane_id": "w1:pTab", "workspace_id": "w1", "agent": "claude",
                                     "tab_id": "w1:t 1;rm"}, {}, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            self.assertIsNone(data["sessions"][self.sid("w1:pTab")]["tab_id"])

    def test_workspace_prefix_authoritative(self):
        """R2 (gap workspace-id-precedence): a mismatched event workspace_id is ignored; the pane prefix is stored."""
        handle_agent_status_changed({"agent_status": "working", "pane_id": "w1:pPre", "workspace_id": "w2", "agent": "claude"},
                                    {}, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            self.assertEqual(data["sessions"][self.sid("w1:pPre")]["workspace_id"], "w1")
        self.assertIn("workspace_id mismatch", (self.state_dir / "plugin.log").read_text())

    def test_colon_less_pane_uses_env_workspace(self):
        """R1 (gap normalize-env-fallback): a colon-less pane without event workspace_id uses HERDR_WORKSPACE_ID."""
        import os
        os.environ["HERDR_WORKSPACE_ID"] = "wEnv"
        handle_agent_status_changed({"agent_status": "working", "pane_id": "pEnv", "agent": "claude"},
                                    {"workspace_id": "wCtx", "focused_pane_id": "pEnv"}, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            self.assertIn(self.sid("wEnv:pEnv"), data["sessions"])
        del os.environ["HERDR_WORKSPACE_ID"]
        handle_agent_status_changed({"agent_status": "working", "pane_id": "pNoWs", "agent": "claude"},
                                    {"workspace_id": "wCtx", "focused_pane_id": "pNoWs"}, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            self.assertFalse(any(k.endswith("pNoWs") for k in data["sessions"]), "context.workspace_id never qualifies a pane")

    def test_long_hostname_truncated_in_session_id(self):
        """Plan §2.3 session_id row (gap hostname-truncation): a 60-char host yields a 32-char host component."""
        from unittest import mock
        with mock.patch("herdr_bartender.sanitize.socket.gethostname", return_value="h" * 60), \
                mock.patch("herdr_bartender.config.socket.gethostname", return_value="h" * 60):
            with self.cache_mgr as data:
                data["host"] = "h" * 60
                self.cache_mgr.save(data)
            handle_agent_status_changed({"agent_status": "working", "pane_id": "w1:pHost", "workspace_id": "w1", "agent": "claude"},
                                        {}, bridge_url=self.mock_url)
        sid = f"herdr:{'h' * 32}:w1:pHost"
        with self.cache_mgr as data:
            self.assertIn(sid, data["sessions"])
        self.assertEqual(self.bridge.history[-1]["session_id"], sid)

    def test_single_marker_for_colon_less_pane(self):
        """Plan L14 (gap marker-dual-link-default): confirmed delivery for a colon-less pane writes exactly one marker."""
        import inspect
        from herdr_bartender.markers import touch_pane_marker
        from herdr_bartender.sanitize import get_hex_pane_id
        handle_agent_status_changed({"agent_status": "working", "pane_id": "pOne", "workspace_id": "w5", "agent": "claude"},
                                    {}, bridge_url=self.mock_url)
        markers = sorted(p.name for p in (self.state_dir / "panes").iterdir() if "." not in p.name)
        self.assertEqual(markers, [get_hex_pane_id("w5:pOne")])
        self.assertNotIn("raw_pane_id", inspect.signature(touch_pane_marker).parameters, "No alias marker parameter")

    # -- close matching -----------------------------------------------------------
    def _admit(self, pane, tab=None, agent="claude"):
        data = {"agent_status": "working", "pane_id": pane, "workspace_id": pane.split(":", 1)[0], "agent": agent}
        if tab:
            data["tab_id"] = tab
        handle_agent_status_changed(data, {}, bridge_url=self.mock_url)

    def _alive(self):
        with self.cache_mgr as data:
            return set(data.get("sessions", {}))

    def test_pane_closed_exact_match(self):
        """Plan §2.1 L151 (gap pane-closed-suffix-match): closing w1:p1 leaves the multi-colon pane x:w1:p1 intact."""
        self._admit("w1:p1")
        self._admit("x:w1:p1")
        handle_pane_closed({"pane_id": "w1:p1", "workspace_id": "w1"}, {}, bridge_url=self.mock_url)
        self.assertEqual(self._alive(), {self.sid("x:w1:p1")})

    def test_pane_closed_without_pane_id_is_noop(self):
        """Plan §2.1 (gap pane-closed-context-leak): pane.closed without pane_id neither crashes nor closes the focused pane."""
        self._admit("w1:pFoc")
        handle_pane_closed({}, {"focused_pane_id": "w1:pFoc", "workspace_id": "w1"}, bridge_url=self.mock_url)
        handle_pane_closed({"pane_id": None}, {}, bridge_url=self.mock_url)
        self.assertEqual(self._alive(), {self.sid("w1:pFoc")})

    def test_background_colon_less_close_ignores_context_workspace(self):
        """Plan §2.3 (gap pane-closed-context-leak): a colon-less pane.closed never resolves via context.workspace_id."""
        self._admit("w9:p1")
        handle_pane_closed({"pane_id": "p1"}, {"focused_pane_id": "w9:p2", "workspace_id": "w9"}, bridge_url=self.mock_url)
        self.assertEqual(self._alive(), {self.sid("w9:p1")})

    def test_close_payload_carries_full_fields(self):
        """Plan §2.3 (gap close-payload-fields): pane.closed sends terminal, event and seq with the Ended payload."""
        self._admit("w1:pPay")
        handle_pane_closed({"pane_id": "w1:pPay", "workspace_id": "w1"}, {}, bridge_url=self.mock_url)
        sent = self.bridge.history[-1]
        self.assertEqual((sent["state"], sent["terminal"], sent["event"], sent["seq"]), ("Ended", "Herdr", "pane.closed", 2))

    def test_tab_closed_exact_match(self):
        """Plan §2.1 L152 (gaps tab-closed-suffix-match, cascade-container-matching): closing w11:t1 keeps w1:t1."""
        self._admit("w1:pT1", tab="w1:t1")
        handle_tab_closed({"tab_id": "w11:t1", "workspace_id": "w11"}, {}, bridge_url=self.mock_url)
        self.assertEqual(self._alive(), {self.sid("w1:pT1")})
        handle_tab_closed({"tab_id": "w1:t1", "workspace_id": "w1"}, {}, bridge_url=self.mock_url)
        self.assertEqual(self._alive(), set())
        self.assertEqual(self.bridge.history[-1]["event"], "tab.closed")

    def test_tab_closed_unqualified_cached_tab(self):
        """Gap cascade-container-matching: a cached unqualified tab 't1' in w1 never matches a closed 'w2:t1'."""
        self._admit("w1:pUq", tab="t1")
        handle_tab_closed({"tab_id": "w2:t1", "workspace_id": "w2"}, {}, bridge_url=self.mock_url)
        self.assertEqual(self._alive(), {self.sid("w1:pUq")})
        handle_tab_closed({"tab_id": "w1:t1"}, {}, bridge_url=self.mock_url)
        self.assertEqual(self._alive(), set())

    def test_container_close_without_id_ignores_focused_context(self):
        """Gap cascade-container-matching: tab/workspace close without its id never falls back to the focused context."""
        self._admit("w1:pCtx", tab="w1:t1")
        ctx = {"focused_pane_id": "w1:pCtx", "tab_id": "w1:t1", "workspace_id": "w1"}
        handle_tab_closed({}, ctx, bridge_url=self.mock_url)
        handle_workspace_closed({}, ctx, bridge_url=self.mock_url)
        self.assertEqual(self._alive(), {self.sid("w1:pCtx")})

    def test_workspace_closed_named_like_host(self):
        """Plan §2.1 L153 (gap tab-closed-suffix-match): workspace.closed with the host's name closes nothing else."""
        self._admit("w1:pHostWs")
        handle_workspace_closed({"workspace_id": self.host}, {}, bridge_url=self.mock_url)
        self.assertEqual(self._alive(), {self.sid("w1:pHostWs")})
        handle_workspace_closed({"workspace_id": "w1"}, {}, bridge_url=self.mock_url)
        self.assertEqual(self._alive(), set())
        self.assertEqual(self.bridge.history[-1]["event"], "workspace.closed")

if __name__ == "__main__":
    unittest.main()
