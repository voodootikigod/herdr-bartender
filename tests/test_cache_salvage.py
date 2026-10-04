"""Corrupt-cache quarantine and salvage."""

import json
import unittest

from herdr_bartender.orphans import run_replay_orphans
from tests.support import SandboxTestCase


class CacheSalvageTests(SandboxTestCase):
    # WEAK: t12-quarantine-signal
    def test_p12_corrupt_cache_quarantine_and_salvage(self):
        """Plan §10.1 #12: a malformed cache is quarantined to .corrupt.<ts> and candidate IDs are salvaged."""
        cache_file = self.state_dir / "active-sessions.json"
        cache_file.write_text('{"sessions": {"herdr:macbook:w1:pSalvage": {"state": "Working"}}, invalid json...')
        with self.cache_mgr as data:
            self.assertIn("herdr:macbook:w1:pSalvage", data.get("sessions", {}), "Expected candidate ID to be salvaged")
        corrupt_files = list(self.state_dir.glob("active-sessions.json.corrupt.*"))
        self.assertGreater(len(corrupt_files), 0, "Expected corrupt cache to be quarantined")

    # WEAK: t50-salvage-hostname
    def test_p50_salvage_preserves_close_envelopes(self):
        """Plan §10.1 #50: salvage keeps close envelopes in spool/, quarantines status envelopes, stages sessions quiescently."""
        spool_salvage = self.state_dir / "spool"
        spool_salvage.mkdir(parents=True, exist_ok=True)
        bad_salvage = spool_salvage / "bad"

        close_env_file = spool_salvage / "00000000000000000001_close.json"
        close_env_file.write_text(json.dumps({"event_name": "pane.closed", "event_data": {"pane_id": "w1:pSalvageClose"}, "context": {}}))
        status_env_file = spool_salvage / "00000000000000000002_status.json"
        status_env_file.write_text(json.dumps({"event_name": "pane.agent_status_changed", "event_data": {"pane_id": "w1:pSalvageStatus"}, "context": {}}))

        # Sandbox: seed and look up the same host (the original hard-coded 'macbook' here).
        self.cache_mgr.cache_file.write_text(
            '{"sessions": {"' + self.sid("w1:pSalvaged") + '": {"pane_id": "w1:pSalvaged"')

        loaded_data = self.cache_mgr._load()
        self.assertTrue(close_env_file.exists(), "Close event envelope must be PRESERVED in spool/ on salvage")
        self.assertFalse(status_env_file.exists(), "Status envelope must be quarantined from spool/")
        self.assertTrue((bad_salvage / status_env_file.name).exists(), "Status envelope must be moved to spool/bad/")

        salvaged_sess = loaded_data["sessions"].get(self.sid("w1:pSalvaged"))
        self.assertIsNotNone(salvaged_sess, "Salvaged session must survive the corrupt-cache load")
        self.assertIs(salvaged_sess["salvaged"], True)
        self.assertEqual(salvaged_sess["desired_state"], "Idle")

    def test_p58_salvage_with_pending_orphans(self):
        """Plan §10.1 #58: an epoch salvage generation does not stop orphan replay from clearing orphans."""
        orphan_test_file = self.state_dir / "test-salvage-orphans.json"
        sid_58 = self.sid("w1:pSalvageOrphan")
        orphan_test_file.write_text(json.dumps({
            "version": 1,
            "sessions": {
                sid_58: {
                    "session_id": sid_58,
                    "pane_id": "w1:pSalvageOrphan",
                    "desired_state": "Ended",
                    "generation": 3,
                    "admitted_at_ns": 500,
                    "agent": "Claude (Herdr)",
                }
            },
        }), encoding="utf-8")
        (self.state_dir / "active-sessions.json").write_text("{corrupt json...", encoding="utf-8")
        with self.cache_mgr as data:
            self.assertGreaterEqual(data.get("next_generation", 0), 1_700_000_000)
        self.bridge.sessions[sid_58] = {"state": "Working"}
        replay_ok = run_replay_orphans(str(orphan_test_file), bridge_url=self.mock_url)
        self.assertIs(replay_ok, True, "Orphan replay must succeed despite epoch salvage generation")
        self.assertNotIn(sid_58, self.bridge.sessions, "Orphan must clear Top Shelf state")
        self.assertFalse(orphan_test_file.exists(), "Orphan file must be cleaned up on success")


if __name__ == "__main__":
    unittest.main()
