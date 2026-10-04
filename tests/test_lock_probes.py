"""No subprocess runs under the cache lock: Step A liveness facts are resolved before locking (Plan §6.1)."""

import time
import unittest
from unittest import mock

from herdr_bartender import process, runtime
from herdr_bartender.handlers import handle_agent_status_changed, handle_tab_closed
from herdr_bartender.process import parse_lstart
from tests.support import SandboxTestCase
from tests.support.sandbox import DEFAULT_BARTENDER_PID, DEFAULT_LSTART

PANE = "w1:pProbe"
WORKING = {"agent_status": "working", "pane_id": PANE, "workspace_id": "w1", "tab_id": "w1:t1", "agent": "claude"}


class NoProbeUnderLockTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.probes = []
        real_run = process.subprocess.run

        def spy(argv, *args, **kwargs):
            self.probes.append((argv[0], runtime.IN_CRITICAL_SECTION))
            return real_run(argv, *args, **kwargs)

        patcher = mock.patch.object(process.subprocess, "run", side_effect=spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _under_lock(self):
        return [name for name, locked in self.probes if locked]

    def _seed_leased_session(self, start_time):
        """A Working session whose lease belongs to another live process (a real child) with ``start_time``."""
        holder = self.add_fake_process("holder", live=True, lstart=DEFAULT_LSTART)
        sid, now = self.sid(PANE), time.time()
        with self.cache_mgr as data:
            data["sessions"][sid] = {
                "pane_id": PANE, "workspace_id": "w1", "tab_id": "w1:t1", "agent": "Claude (Herdr)",
                "desired_state": "Working", "seq": 1, "delivered_seq": 0, "generation": 1, "admitted_at_ns": 1,
                "desired_payload": {"state": "Working", "agent": "Claude (Herdr)", "session_id": sid, "seq": 1},
                "lease_token": f"{holder}:{start_time}:{now}:{sid}", "sending_pid": holder,
                "lease_deadline": now + 1.5, "delivery_status": "in_flight", "last_arrival_ns": 1,
                "last_event_ns": 1, "last_applied_arrival_time": 0.0,
            }
            data["pane_generations"][PANE] = 1
            self.cache_mgr.save(data)
        self.probes.clear()
        return sid

    def test_live_lease_holder_defers_without_probing_under_lock(self):
        """Plan §6.1 L725 (finding: probes under the lock): the holder's start time is resolved before Step A
        locks; under the lock the lease check only reads the memo. A live holder (same instance) defers."""
        sid = self._seed_leased_session(parse_lstart(DEFAULT_LSTART))
        handle_agent_status_changed({**WORKING, "agent_status": "blocked"}, {}, bridge_url=self.mock_url)
        self.assertEqual(self._under_lock(), [])
        self.assertIn("ps", [name for name, _ in self.probes], "the holder start time was resolved before locking")
        self.assertEqual(self.bridge.requests, [], "a live lease elsewhere defers the send")
        with self.cache_mgr as data:
            self.assertEqual(data["sessions"][sid]["seq"], 2)
        self.assertTrue((self.state_dir / "reconciler.pending").exists())

    def test_reused_pid_is_taken_over_using_the_prewarmed_start_time(self):
        """R14 (finding: probes under the lock): a holder PID that is alive but a different instance (start times
        known and different) is taken over; the decision uses the start time resolved before locking."""
        self._seed_leased_session("1234")
        handle_agent_status_changed({**WORKING, "agent_status": "blocked"}, {}, bridge_url=self.mock_url)
        self.assertEqual(self._under_lock(), [])
        self.assertEqual(len(self.bridge.requests), 1, "the stale lease was taken over and the state sent")

    def test_unwarmed_lease_check_never_spawns_and_stays_conservative(self):
        """R14 + Plan §6.1: if the holder was not resolved before locking (the lease appeared after the pre-lock
        peek), the check under the lock still spawns nothing and treats the live holder as active (defer)."""
        self._seed_leased_session("1234")
        with mock.patch("herdr_bartender.handlers.flow.warm_step_a_probes"):
            handle_agent_status_changed({**WORKING, "agent_status": "blocked"}, {}, bridge_url=self.mock_url)
        self.assertEqual(self.probes, [])
        self.assertEqual(self.bridge.requests, [], "an unresolved start time never rejects a live holder")

    def _seed_tombstone(self):
        with self.cache_mgr as data:
            data["tombstones"][PANE] = {"closed_at_ns": time.time_ns() - 1_000_000_000,
                                        "closed_source_ts": 0.0, "last_source_timestamp": 0.0}
            self.cache_mgr.save(data)
        self.probes.clear()

    def test_tombstone_gate_admits_with_prewarmed_herdr_liveness(self):
        """Plan §4.1 tombstone gate (finding: probes under the lock): Herdr liveness is probed before the lock;
        a working event with an agent re-admits a recently closed pane while Herdr runs."""
        self._seed_tombstone()
        handle_agent_status_changed(WORKING, {}, bridge_url=self.mock_url)
        self.assertEqual(self._under_lock(), [])
        self.assertIn("pgrep", [name for name, _ in self.probes])
        with self.cache_mgr as data:
            self.assertIn(self.sid(PANE), data["sessions"])
            self.assertNotIn(PANE, data["tombstones"])

    def test_tombstone_gate_rejects_when_prewarmed_herdr_is_dead(self):
        """Plan §4.1 tombstone gate: with Herdr known dead (probed before the lock) the event is rejected."""
        self.clear_fake_processes()
        self.add_fake_process("Bartender 6", pid=DEFAULT_BARTENDER_PID)
        self._seed_tombstone()
        handle_agent_status_changed(WORKING, {}, bridge_url=self.mock_url)
        self.assertEqual(self._under_lock(), [])
        with self.cache_mgr as data:
            self.assertNotIn(self.sid(PANE), data["sessions"])
            self.assertIn(PANE, data["tombstones"])

    def test_cascade_lease_check_runs_no_probe_under_lock(self):
        """Plan §6.1 (finding: probes under the lock): close Step A lease checks read only the memo; a session
        leased by a live holder is left to the reconciler."""
        sid = self._seed_leased_session(parse_lstart(DEFAULT_LSTART))
        handle_tab_closed({"tab_id": "w1:t1", "workspace_id": "w1"}, {}, bridge_url=self.mock_url)
        self.assertEqual(self._under_lock(), [])
        self.assertEqual(self.bridge.requests, [])
        with self.cache_mgr as data:
            self.assertEqual(data["sessions"][sid]["desired_state"], "Ended")


if __name__ == "__main__":
    unittest.main()
