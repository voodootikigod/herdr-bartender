"""Generation counters and arrival ordering."""

import time
import unittest

from herdr_bartender.handlers import handle_agent_status_changed, handle_pane_closed
from tests.support import SandboxTestCase


class GenerationTests(SandboxTestCase):
    # WEAK: t35-backward-clock
    def test_p35_generation_monotonic_under_backward_clock(self):
        """Plan §10.1 #35: re-admission after Ended bumps generation and cache_seq despite a backward wall clock."""
        now_tt = time.time()
        sid_gen = self.sid("w1:pGenTest")
        with self.cache_mgr as data:
            data["sessions"][sid_gen] = {
                "generation": 1, "seq": 1, "delivered_seq": 1,
                "desired_state": "Ended", "delivered_state": "Ended",
                "pane_id": "w1:pGenTest", "agent": "Claude (Herdr)", "last_event_at": now_tt,
            }
            self.cache_mgr.save(data)
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pGenTest", "workspace_id": "w1", "agent": "claude"},
            {}, bridge_url=self.mock_url, arrival_time=now_tt - 100.0, arrival_ns=time.time_ns(),
        )
        with self.cache_mgr as data:
            s_gen = data["sessions"][sid_gen]
            self.assertGreater(s_gen.get("generation"), 1, f"Generation must increment monotonically, got {s_gen.get('generation')}")
            self.assertGreater(data.get("cache_seq", 0), 1, "Cache seq must increment monotonically")

    def test_p38_pane_generation_survives_eviction(self):
        """Plan §10.1 #38: pane_generations persists across eviction and the next turn increments it."""
        sid_pgen = self.sid("w1:pPersistGen")
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pPersistGen", "workspace_id": "w1", "agent": "claude"},
            {}, bridge_url=self.mock_url,
        )
        with self.cache_mgr as data:
            gen_1 = data["sessions"][sid_pgen]["generation"]
            self.assertGreaterEqual(gen_1, 1)
            self.assertEqual(data["pane_generations"]["w1:pPersistGen"], gen_1)

        handle_pane_closed({"pane_id": "w1:pPersistGen", "workspace_id": "w1"}, {}, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            self.assertNotIn(sid_pgen, data["sessions"], "Session must be evicted on confirmed Ended")
            self.assertEqual(data["pane_generations"]["w1:pPersistGen"], gen_1, "Pane generation must persist across eviction")

        t_pgen_reopen = time.time_ns()
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pPersistGen", "workspace_id": "w1", "agent": "claude",
             "timestamp": (t_pgen_reopen + 1_000_000_000) / 1e9},
            {}, bridge_url=self.mock_url, arrival_ns=t_pgen_reopen + 1_000_000_000,
        )
        with self.cache_mgr as data:
            gen_2 = data["sessions"][sid_pgen]["generation"]
            self.assertGreater(gen_2, gen_1, f"New session turn must increment generation ({gen_2} > {gen_1})")
            self.assertEqual(data["pane_generations"]["w1:pPersistGen"], gen_2)

    def test_p39_stale_agent_exit_dropped_by_arrival(self):
        """Plan §10.1 #39: an agent-exit event with an older arrival time does not end the newer session."""
        sid_stale = self.sid("w1:pStaleExit")
        t_stale_now = time.time_ns()
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pStaleExit", "workspace_id": "w1", "agent": "claude"},
            {}, bridge_url=self.mock_url, arrival_ns=t_stale_now,
        )
        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pStaleExit", "workspace_id": "w1", "agent": "claude"},
            {}, bridge_url=self.mock_url, arrival_ns=t_stale_now + 2000,
        )
        with self.cache_mgr as data:
            self.assertEqual(data["sessions"][sid_stale]["desired_state"], "Working")

        handle_agent_status_changed(
            {"agent_status": "idle", "pane_id": "w1:pStaleExit", "workspace_id": "w1", "agent": ""},
            {}, bridge_url=self.mock_url, arrival_ns=t_stale_now + 1000,
        )
        with self.cache_mgr as data:
            self.assertEqual(data["sessions"][sid_stale]["desired_state"], "Working", "Stale agent-exit event must be dropped by arrival ordering")

    def test_p49_next_generation_root_counter(self):
        """Plan §10.1 #49: next_generation at the cache root survives pruning of pane_generations."""
        with self.cache_mgr as data:
            data["next_generation"] = 5
            data["pane_generations"] = {}
            self.cache_mgr.save(data)

        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pNextGen", "workspace_id": "w1", "agent": "claude"},
            {}, bridge_url=self.mock_url,
        )
        with self.cache_mgr as data:
            s_ngen = data["sessions"][self.sid("w1:pNextGen")]
            self.assertEqual(s_ngen["generation"], 6, f"Generation must be max(next_gen=5, ...) + 1 = 6, got {s_ngen['generation']}")
            self.assertEqual(data["next_generation"], 6)
            self.assertEqual(data["pane_generations"]["w1:pNextGen"], 6)


if __name__ == "__main__":
    unittest.main()
