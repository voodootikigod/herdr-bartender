"""Spool directory: enqueue, replay ordering and poison-pill quarantine."""

import json
import time
import unittest

from herdr_bartender.handlers import handle_agent_status_changed
from herdr_bartender.spool import enqueue_spool, replay_spool_dir
from tests.support import SandboxTestCase


class SpoolTests(SandboxTestCase):
    # WEAK: t10-spool-fifo
    def test_p10_spool_enqueue_ingest_and_arrival_ordering(self):
        """Plan §10.1 #10: spooled envelopes are ingested and an older spooled event cannot overwrite newer state."""
        spool_dir = self.state_dir / "spool"
        enqueue_spool("direct_post", {"state": "Working", "agent": "SpoolAgent", "session_id": "spool-session-1"}, {})
        enqueue_spool("direct_post", {"state": "Done", "agent": "SpoolAgent", "session_id": "spool-session-1"}, {})
        spool_files = list(spool_dir.glob("*.json"))
        self.assertEqual(len(spool_files), 2, f"Expected 2 spooled files, got {len(spool_files)}")
        replay_spool_dir(self.state_dir, bridge_url=self.mock_url)
        self.assertEqual(len(list(spool_dir.glob("*.json"))), 0, "All spool files should be ingested and unlinked")
        spool_events = self.bridge.events_for("spool-session-1")
        self.assertEqual(len(spool_events), 2, f"Expected 2 spooled events delivered, got {len(spool_events)}")

        t_arr = time.time()
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pArrival", "workspace_id": "w1", "agent": "claude"},
            {}, bridge_url=self.mock_url, arrival_time=t_arr,
        )
        enqueue_spool(
            "pane.agent_status_changed",
            {"agent_status": "idle", "pane_id": "w1:pArrival", "workspace_id": "w1", "agent": "claude"},
            {}, arrival_time=t_arr - 5.0,
        )
        replay_spool_dir(self.state_dir, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            s_arr = data["sessions"].get(self.sid("w1:pArrival"))
            self.assertTrue(s_arr and s_arr["desired_state"] == "Working", "Older spooled event must not overwrite newer state")

    def test_p10_spooled_close_exempt_from_arrival_drops(self):
        """Plan §10.1 #10: a spooled pane.closed with an older arrival time is still applied (never dropped)."""
        t_close_arr = time.time()
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pCloseTest", "workspace_id": "w1", "agent": "claude"},
            {}, bridge_url=self.mock_url, arrival_time=t_close_arr,
        )
        enqueue_spool("pane.closed", {"pane_id": "w1:pCloseTest", "workspace_id": "w1"}, {},
                      arrival_time=t_close_arr - 10.0)
        replay_spool_dir(self.state_dir, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            s_closed = data["sessions"].get(self.sid("w1:pCloseTest"))
            self.assertTrue(s_closed is None or s_closed.get("desired_state") == "Ended", "Close event must never be dropped by arrival time")

    def test_p28_spool_poison_pill_quarantine(self):
        """Plan §10.1 #28: a malformed envelope moves to spool/bad/ and the next valid envelope still delivers."""
        spool_dir = self.state_dir / "spool"
        bad_dir = spool_dir / "bad"
        spool_dir.mkdir(parents=True, exist_ok=True)

        bad_spool_file = spool_dir / "00000000000000000001_poison.json"
        bad_spool_file.write_text("{malformed_json_corrupt", encoding="utf-8")
        good_spool_file = spool_dir / "00000000000000000002_valid.json"
        good_envelope = {
            "event_name": "direct_post",
            "event_data": {"state": "Working", "agent": "Claude (Herdr)", "session_id": "herdr:spool:good1"},
            "context": {},
            "arrival_time": time.time(),
            "arrival_ns": time.time_ns(),
            "enqueued_at": time.time(),
            "enqueued_ns": time.time_ns(),
        }
        good_spool_file.write_text(json.dumps(good_envelope), encoding="utf-8")

        replay_spool_dir(self.state_dir, bridge_url=self.mock_url, max_batch=16)
        self.assertFalse(bad_spool_file.exists(), "Corrupt spool file must be moved out of spool directory")
        self.assertTrue((bad_dir / bad_spool_file.name).exists(), "Corrupt spool file must be quarantined to spool/bad/")
        self.assertFalse(good_spool_file.exists(), "Valid spool file must be processed and removed")
        self.assertIn("herdr:spool:good1", self.bridge.sessions, "Subsequent valid spool envelope must be delivered")


if __name__ == "__main__":
    unittest.main()
