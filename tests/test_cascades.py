"""Container closes (pane/tab/workspace) and the tombstone table."""

import time
import unittest

from herdr_bartender.handlers import (
    handle_agent_status_changed,
    handle_pane_closed,
    handle_tab_closed,
    handle_workspace_closed,
)
from tests.support import SandboxTestCase


class CascadeTests(SandboxTestCase):
    def test_p03_container_cascades(self):
        """Plan §10.1 #3: pane.closed, tab.closed and workspace.closed end their matching sessions (lease
        deferral, overflow and budget rules: tests/test_cascade_protocol.py)."""
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:p1", "workspace_id": "w1", "agent": "claude", "title": "Unit test turn"},
            {"tab_id": "w1:t1"},
            bridge_url=self.mock_url,
        )
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "wTest:pA", "workspace_id": "wTest", "tab_id": "wTest:tA", "agent": "codex"},
            {"focused_pane_id": "wTest:pA", "tab_id": "wTest:tA"},
            bridge_url=self.mock_url,
        )
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "wTest:pB", "workspace_id": "wTest", "tab_id": "wTest:tB", "agent": "claude"},
            {"focused_pane_id": "wTest:pB", "tab_id": "wTest:tB"},
            bridge_url=self.mock_url,
        )
        self.assertEqual(len(self.bridge.sessions), 3)

        handle_tab_closed({"tab_id": "wTest:tA"}, {}, bridge_url=self.mock_url)
        self.assertEqual(len(self.bridge.sessions), 2)

        handle_workspace_closed({"workspace_id": "wTest"}, {}, bridge_url=self.mock_url)
        self.assertEqual(len(self.bridge.sessions), 1)

        handle_pane_closed({"pane_id": "w1:p1", "workspace_id": "w1"}, {}, bridge_url=self.mock_url)
        self.assertEqual(len(self.bridge.sessions), 0)

    def test_p24_tombstone_rejects_late_status(self):
        """Plan §10.1 #24: a status event arriving at/before the tombstone cannot resurrect the pane."""
        t_close = time.time_ns()
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pTombstone", "workspace_id": "w1", "agent": "claude"},
            {}, bridge_url=self.mock_url, arrival_ns=t_close,
        )
        handle_pane_closed({"pane_id": "w1:pTombstone", "workspace_id": "w1"}, {},
                           bridge_url=self.mock_url, arrival_ns=t_close + 10_000)
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pTombstone", "workspace_id": "w1", "agent": "claude"},
            {}, bridge_url=self.mock_url, arrival_ns=t_close + 5_000,
        )
        with self.cache_mgr as data:
            self.assertNotIn(self.sid("w1:pTombstone"), data.get("sessions", {}), "Late status event must not resurrect closed pane")
            self.assertIn("w1:pTombstone", data.get("tombstones", {}), "Tombstone must be recorded")

    def test_p27_close_vs_status_positive_admission(self):
        """Plan §10.1 #27: within 60s of close only a positive, post-close agent event pops the tombstone."""
        t_race_close = time.time_ns()
        handle_pane_closed({"pane_id": "w1:pRace", "workspace_id": "w1"}, {},
                           bridge_url=self.mock_url, arrival_ns=t_race_close)
        with self.cache_mgr as data:
            self.assertIn("w1:pRace", data.get("tombstones", {}))

        handle_agent_status_changed(
            {"agent_status": "idle", "pane_id": "w1:pRace", "workspace_id": "w1", "agent": ""},
            {}, bridge_url=self.mock_url, arrival_ns=t_race_close + 1_000,
        )
        sid_race = self.sid("w1:pRace")
        with self.cache_mgr as data:
            self.assertNotIn(sid_race, data.get("sessions", {}), "Unassisted status without agent must not pop tombstone")
            self.assertIn("w1:pRace", data.get("tombstones", {}))

        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pRace", "workspace_id": "w1", "agent": "claude", "timestamp": t_race_close / 1e9},
            {}, bridge_url=self.mock_url, arrival_ns=t_race_close + 500_000_000,
        )
        with self.cache_mgr as data:
            self.assertNotIn(sid_race, data.get("sessions", {}), "Pre-close status with timestamp <= closed_source_ts must not pop tombstone")
            self.assertIn("w1:pRace", data.get("tombstones", {}))

        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pRace", "workspace_id": "w1", "agent": "claude",
             "timestamp": (t_race_close + 2_000_000_000) / 1e9},
            {}, bridge_url=self.mock_url, arrival_ns=t_race_close + 2_000_000_000,
        )
        with self.cache_mgr as data:
            self.assertIn(sid_race, data.get("sessions", {}), "Positive admission signal must admit new session")
            self.assertNotIn("w1:pRace", data.get("tombstones", {}), "Tombstone must be cleared on positive admission")

    def test_p40_tombstone_source_timestamp_rejection(self):
        """Plan §10.1 #40: an event with timestamp <= the tombstone's last_source_timestamp is rejected."""
        sid_ts = self.sid("w1:pTombSrc")
        t_ts_now = time.time_ns()
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pTombSrc", "workspace_id": "w1", "agent": "claude", "timestamp": 5000.0},
            {}, bridge_url=self.mock_url, arrival_ns=t_ts_now,
        )
        handle_pane_closed({"pane_id": "w1:pTombSrc", "workspace_id": "w1"}, {},
                           bridge_url=self.mock_url, arrival_ns=t_ts_now + 10_000)
        with self.cache_mgr as data:
            self.assertIn("w1:pTombSrc", data["tombstones"])
            self.assertEqual(data["tombstones"]["w1:pTombSrc"]["last_source_timestamp"], 5000.0)

        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pTombSrc", "workspace_id": "w1", "agent": "claude", "timestamp": 4999.0},
            {}, bridge_url=self.mock_url, arrival_ns=t_ts_now + 20_000,
        )
        with self.cache_mgr as data:
            self.assertNotIn(sid_ts, data["sessions"], "Trailing event with source ts <= tombstone ts must be rejected")
            self.assertIn("w1:pTombSrc", data["tombstones"])


if __name__ == "__main__":
    unittest.main()
