"""pane.agent_status_changed intake: lifecycle, anti-flap, ordering, context isolation."""

import time
import unittest

from herdr_bartender.handlers import handle_agent_status_changed
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

    # WEAK: t2-lifecycle
    def test_p02_lifecycle_transitions(self):
        """Plan §10.1 #2: working -> Working, then blocked -> Waiting, delivered to the bridge."""
        self._lifecycle_prefix()

    def test_p05_unknown_status_debounced(self):
        """Plan §10.1 #5: an `unknown` status does not evict the active session (anti-flap)."""
        self._lifecycle_prefix()
        handle_agent_status_changed(
            {"agent_status": "unknown", "pane_id": "w1:p1", "workspace_id": "w1"},
            {"tab_id": "w1:t1"},
            bridge_url=self.mock_url,
        )
        self.assertEqual(len(self.bridge.sessions), 1)

    # WEAK: t7-seq-monotonic
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


if __name__ == "__main__":
    unittest.main()
