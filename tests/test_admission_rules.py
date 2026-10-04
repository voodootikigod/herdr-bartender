"""Plan §4.1 / §4.3 Step A live evaluation rules exercised through the real status handler.

Gaps: source-staleness-tolerance, last-event-ns-clamp (R4), generation-bump-on-delivered-ended,
agent-exits-prune-and-pop, capacity-prune-before-reject (undelivered Ended journaled before the save that prunes it),
closed-source-ts-not-persisted (R7 tombstone bounds survive the Step C confirmation).
"""

import json
import time
import unittest
from unittest import mock

from herdr_bartender.handlers import flow, handle_agent_status_changed, handle_pane_closed
from herdr_bartender.orphans import flush_pending_orphan_ops
from herdr_bartender.paths import get_orphan_path
from herdr_bartender.sender import step_a
from tests.support import SandboxTestCase

PANE = "w1:pRule"


def _event(status="working", agent="claude", **extra):
    return {"agent_status": status, "pane_id": PANE, "workspace_id": "w1", "agent": agent, **extra}


class SourceStalenessToleranceTests(SandboxTestCase):
    """Plan §4.3 L425: drop only when ``timestamp < last_source_timestamp - 0.1`` and ``timestamp is not None``."""

    def _session(self):
        with self.cache_mgr as data:
            return data["sessions"][self.sid(PANE)]

    def test_slightly_older_source_timestamp_is_accepted(self):
        handle_agent_status_changed(_event(timestamp=1000.0), {}, bridge_url=self.mock_url)
        handle_agent_status_changed(_event("blocked", timestamp=999.95), {}, bridge_url=self.mock_url)
        session = self._session()
        self.assertEqual((session["desired_state"], session["seq"]), ("Waiting", 2), "50ms older is within tolerance")
        self.assertEqual(session["last_source_timestamp"], 1000.0, "last_source_timestamp never moves backward")

    def test_older_than_tolerance_is_dropped(self):
        handle_agent_status_changed(_event(timestamp=1000.0), {}, bridge_url=self.mock_url)
        handle_agent_status_changed(_event("blocked", timestamp=999.8), {}, bridge_url=self.mock_url)
        session = self._session()
        self.assertEqual((session["desired_state"], session["seq"]), ("Working", 1))

    def test_zero_timestamp_is_a_present_timestamp(self):
        """``is not None`` (not truthiness): a 0.0 timestamp is evaluated, and dropped against a later source."""
        handle_agent_status_changed(_event(timestamp=1000.0), {}, bridge_url=self.mock_url)
        handle_agent_status_changed(_event("blocked", timestamp=0.0), {}, bridge_url=self.mock_url)
        self.assertEqual(self._session()["desired_state"], "Working")


class LastEventNsClampTests(SandboxTestCase):
    """R4: ``last_event_ns = max(arr_ns, prev + 1)``; ``last_arrival_ns`` stays the raw arrival."""

    def test_equal_arrival_still_advances_last_event_ns(self):
        arr = time.time_ns()
        handle_agent_status_changed(_event(), {}, bridge_url=self.mock_url, arrival_ns=arr)
        handle_agent_status_changed(_event("blocked"), {}, bridge_url=self.mock_url, arrival_ns=arr)
        with self.cache_mgr as data:
            session = data["sessions"][self.sid(PANE)]
        self.assertEqual(session["seq"], 2)
        self.assertEqual(session["last_event_ns"], arr + 1)
        self.assertEqual(session["last_arrival_ns"], arr)

    def test_clamped_value_dominates_a_backward_arrival(self):
        arr = time.time_ns()
        handle_agent_status_changed(_event(), {}, bridge_url=self.mock_url, arrival_ns=arr)
        with self.cache_mgr as data:
            data["sessions"][self.sid(PANE)]["last_event_ns"] = arr + 5_000_000_000  # an earlier clock step forward
            self.cache_mgr.save(data)
        handle_agent_status_changed(_event("blocked"), {}, bridge_url=self.mock_url, arrival_ns=arr + 10)
        with self.cache_mgr as data:
            session = data["sessions"][self.sid(PANE)]
        self.assertEqual(session["last_event_ns"], arr + 5_000_000_001)
        self.assertEqual(session["last_arrival_ns"], arr + 10)


class GenerationBumpTests(SandboxTestCase):
    """Gap generation-bump-on-delivered-ended: bump only for a new record or a desired Ended -> live transition."""

    def test_live_record_with_delivered_ended_keeps_its_generation(self):
        sid, admitted = self.sid(PANE), time.time_ns() - 1_000_000
        with self.cache_mgr as data:
            data["sessions"][sid] = {
                "pane_id": PANE, "workspace_id": "w1", "agent": "Claude (Herdr)", "raw_agent": "claude",
                "desired_state": "Working", "delivered_state": "Ended", "seq": 3, "delivered_seq": 2,
                "generation": 5, "admitted_at_ns": admitted, "last_arrival_ns": admitted,
                "last_event_ns": admitted, "delivery_status": "in_flight", "last_event_at": time.time(),
            }
            data["pane_generations"][PANE] = 5
            data["next_generation"] = 5
            self.cache_mgr.save(data)
        handle_agent_status_changed(_event("blocked"), {}, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            session = data["sessions"][sid]
            self.assertEqual((session["generation"], session["admitted_at_ns"]), (5, admitted))
            self.assertEqual(data["next_generation"], 5)
            self.assertEqual(session["seq"], 4)

    def test_desired_ended_to_live_bumps_generation(self):
        sid = self.sid(PANE)
        with self.cache_mgr as data:
            data["sessions"][sid] = {"pane_id": PANE, "desired_state": "Ended", "seq": 2, "delivered_seq": 1,
                                     "generation": 5, "admitted_at_ns": 1, "last_event_at": time.time()}
            data["next_generation"] = 5
            self.cache_mgr.save(data)
        handle_agent_status_changed(_event(), {}, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            self.assertEqual(data["sessions"][sid]["generation"], 6)


class AgentExitsTests(SandboxTestCase):
    """Plan §4.3 L421-424 / L303 (gap agent-exits-prune-and-pop)."""

    def _seed_exit(self, age_s: float):
        with self.cache_mgr as data:
            data["agent_exits"][PANE] = {"exit_at_ns": time.time_ns() - int(age_s * 1e9), "exit_source_ts": 0.0}
            self.cache_mgr.save(data)

    def _exits(self):
        with self.cache_mgr as data:
            return dict(data["agent_exits"]), dict(data["sessions"])

    def test_working_without_event_agent_does_not_pop(self):
        """A focused working event admits through focused_pane_agent but cannot clear agent_exits."""
        self._seed_exit(1.0)
        ctx = {"focused_pane_id": PANE, "focused_pane_agent": "claude"}
        handle_agent_status_changed(_event(agent=None), ctx, bridge_url=self.mock_url)
        exits, sessions = self._exits()
        self.assertIn(self.sid(PANE), sessions, "fresh focused working event is admitted")
        self.assertIn(PANE, exits, "only a working event with event.data.agent pops the agent_exits entry")

    def test_working_with_event_agent_pops(self):
        self._seed_exit(1.0)
        handle_agent_status_changed(_event(), {}, bridge_url=self.mock_url)
        exits, _ = self._exits()
        self.assertNotIn(PANE, exits)

    def test_entry_exactly_60s_old_still_applies(self):
        """The 60s TTL expires an entry only once it is MORE than 60s old: at exactly 60s a stale event is dropped."""
        arr = self.use_fake_clock().time_ns()  # frozen: the entry is exactly 60s old at seeding and at arrival
        with self.cache_mgr as data:
            data["agent_exits"][PANE] = {"exit_at_ns": arr - 60_000_000_000, "exit_source_ts": 500.0}
            self.cache_mgr.save(data)
        handle_agent_status_changed(_event(timestamp=400.0), {}, bridge_url=self.mock_url, arrival_ns=arr)
        exits, sessions = self._exits()
        self.assertNotIn(self.sid(PANE), sessions, "source ts <= exit_source_ts: dropped")
        self.assertIn(PANE, exits)

    def test_entries_older_than_60s_are_pruned_on_save(self):
        self._seed_exit(61.0)
        with self.cache_mgr as data:
            data["agent_exits"]["w9:pFresh"] = {"exit_at_ns": time.time_ns(), "exit_source_ts": 0.0}
            self.cache_mgr.save(data)
        exits, _ = self._exits()
        self.assertNotIn(PANE, exits)
        self.assertIn("w9:pFresh", exits)


class TombstoneOriginTests(SandboxTestCase):
    """R7 / gap closed-source-ts-not-persisted: the confirmed close re-affirms the tombstone without weakening it."""

    def test_confirmed_close_keeps_the_event_source_timestamp(self):
        handle_agent_status_changed(_event(timestamp=100.0), {}, bridge_url=self.mock_url)
        arr = time.time_ns()
        handle_pane_closed({"pane_id": PANE, "timestamp": 200.0}, {}, bridge_url=self.mock_url, arrival_ns=arr)
        with self.cache_mgr as data:
            self.assertNotIn(self.sid(PANE), data["sessions"], "the Ended was confirmed")
            tomb = data["tombstones"][PANE]
        self.assertEqual(tomb["closed_at_ns"], arr)
        self.assertEqual(tomb["last_source_timestamp"], 200.0, "max(session ts, close event ts) survives Step C")
        self.assertEqual(tomb["closed_source_ts"], max(200.0, arr / 1e9))


class CapacityAdmissionTests(SandboxTestCase):
    """Plan §4.1 L290-293 / §4.3 L426-429: Ended and salvaged records are pruned before session 257 is refused."""

    def _fill(self, extra_sid, extra):
        live = {f"herdr:{self.host}:wCap:p{i}": {"desired_state": "Working", "seq": 1, "pane_id": f"wCap:p{i}",
                                                  "agent": "Claude (Herdr)", "last_event_at": time.time()}
                for i in range(255)}
        with self.cache_mgr as data:
            data["sessions"] = {**live, extra_sid: extra}
            self.cache_mgr.save(data)

    def _admit(self):
        handle_agent_status_changed({"agent_status": "working", "pane_id": "wCap:p257", "workspace_id": "wCap",
                                     "agent": "claude"}, {}, bridge_url=self.mock_url)

    def test_delivered_ended_is_pruned_to_admit(self):
        ended = self.sid("wOld:pDone")
        self._fill(ended, {"desired_state": "Ended", "delivered_state": "Ended", "seq": 2, "delivered_seq": 2,
                           "pane_id": "wOld:pDone", "last_event_at": 1.0})
        self._admit()
        with self.cache_mgr as data:
            self.assertIn(self.sid("wCap:p257"), data["sessions"])
            self.assertNotIn(ended, data["sessions"])
            self.assertEqual(len(data["sessions"]), 256)

    def test_undelivered_ended_is_exported_then_pruned(self):
        """Zero-data-loss: an Ended still owed to Bartender is mirrored to the orphan file before it makes room."""
        owed = self.sid("wOld:pOwed")
        self._fill(owed, {"desired_state": "Ended", "seq": 2, "delivered_seq": 1, "delivery_status": "in_flight",
                          "pane_id": "wOld:pOwed", "agent": "Claude (Herdr)", "last_event_at": 1.0})
        self._admit()
        with self.cache_mgr as data:
            self.assertIn(self.sid("wCap:p257"), data["sessions"])
            self.assertNotIn(owed, data["sessions"])
        exported = json.loads(get_orphan_path().read_text())["sessions"]
        self.assertIn(owed, exported)
        self.assertIs(exported[owed]["orphaned_ended"], True)

    def _fill_owed(self):
        owed = self.sid("wOld:pOwed")
        self._fill(owed, {"desired_state": "Ended", "seq": 2, "delivered_seq": 1, "delivery_status": "in_flight",
                          "pane_id": "wOld:pOwed", "agent": "Claude (Herdr)", "last_event_at": 1.0})
        return owed

    def test_pruned_undelivered_ended_survives_a_crash_right_after_the_save(self):
        """The prune is saved in Step A; the owed Ended must already be mirrored durably by then (journaled before
        the save), so a process killed right after the save loses nothing."""
        owed = self._fill_owed()
        with mock.patch.object(step_a, "has_live_sessions", side_effect=SystemExit(0)):  # dies after the save
            with self.assertRaises(SystemExit):
                self._admit()
        with self.cache_mgr as data:
            self.assertNotIn(owed, data["sessions"], "the prune was saved")
        self.assertTrue(flush_pending_orphan_ops(blocking=True))
        exported = json.loads(get_orphan_path().read_text())["sessions"]
        self.assertIs(exported[owed]["orphaned_ended"], True)

    def test_unjournaled_prune_is_never_saved(self):
        """A pruned Ended that cannot be mirrored durably aborts Step A: nothing is pruned or sent, the event spools."""
        owed = self._fill_owed()
        with mock.patch.object(step_a, "journal_orphan_exports", side_effect=OSError("ENOSPC")):
            self._admit()
        with self.cache_mgr as data:
            self.assertIn(owed, data["sessions"])
            self.assertNotIn(self.sid("wCap:p257"), data["sessions"])
        self.assertEqual(len(list((self.state_dir / "spool").glob("*.json"))), 1)
        self.assertEqual(self.bridge.requests, [])

    def test_contended_orphan_fold_hands_the_journal_to_the_reconciler(self):
        owed = self._fill_owed()
        with mock.patch.object(flow, "flush_pending_orphan_ops", return_value=False):
            self._admit()
        journal = get_orphan_path().with_name(get_orphan_path().name + ".pending")
        (entry,) = journal.glob("*.json")
        self.assertEqual(json.loads(entry.read_text())["sid"], owed)
        self.assertTrue((self.state_dir / "reconciler.pending").exists())
        self.assertTrue(self.spawner.calls)

    def test_ended_leased_by_another_live_sender_is_not_pruned(self):
        """An Ended another sender is delivering right now (open lease) is skipped; the next candidate makes room."""
        leased, spare = self.sid("wOld:pLeased"), self.sid("wOld:pSpare")
        ended = {"desired_state": "Ended", "seq": 2, "delivered_seq": 1, "delivery_status": "in_flight",
                 "agent": "Claude (Herdr)"}
        self._fill(leased, {**ended, "pane_id": "wOld:pLeased", "last_event_at": 1.0, "sending_pid": 4242,
                            "lease_token": "4242:None:1.0:x", "lease_deadline": time.time() + 1.5})
        with self.cache_mgr as data:
            data["sessions"].pop(self.sid("wCap:p0"))
            data["sessions"][spare] = {**ended, "pane_id": "wOld:pSpare", "last_event_at": 2.0}
            self.cache_mgr.save(data)
        self._admit()
        with self.cache_mgr as data:
            self.assertIn(self.sid("wCap:p257"), data["sessions"])
            self.assertIn(leased, data["sessions"], "the leased Ended is not pruned")
            self.assertNotIn(spare, data["sessions"])

    def test_all_live_still_refuses_257(self):
        self._fill(self.sid("wCap:p255"), {"desired_state": "Idle", "seq": 1, "pane_id": "wCap:p255",
                                           "last_event_at": time.time()})
        self._admit()
        with self.cache_mgr as data:
            self.assertNotIn(self.sid("wCap:p257"), data["sessions"])
            self.assertEqual(len(data["sessions"]), 256)
        self.assertEqual(self.bridge.requests, [])


if __name__ == "__main__":
    unittest.main()
