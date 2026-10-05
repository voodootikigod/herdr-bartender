"""Shared Step C apply-result function (Plan §3.3 response matrix, §4.3 Step C branches)."""

import time
import unittest

from herdr_bartender.cache_schema import new_cache
from herdr_bartender.delivery_state import (
    Outcome,
    Transmission,
    apply_delivery_result,
    commit_delivery_down,
    stage_vendor_cleanup,
)
from herdr_bartender.markers import is_delivery_down, touch_pane_failed
from herdr_bartender.sanitize import get_hex_pane_id
from tests.support import SandboxTestCase

PANE = "w1:pApply"
TOKEN = "123:456:1.0:sid"


class ApplyDeliveryResultTests(SandboxTestCase):
    start_bridge = False

    def setUp(self):
        super().setUp()
        self.sid_ = self.sid(PANE)
        self.data = new_cache(self.host)
        self.data["pane_generations"][PANE] = 7
        self.data["sessions"][self.sid_] = {
            "pane_id": PANE, "agent": "Claude (Herdr)", "desired_state": "Working",
            "desired_payload": {"state": "Working", "agent": "Claude (Herdr)", "session_id": self.sid_, "seq": 2},
            "seq": 2, "delivered_seq": 1, "rejected_seq": 0, "generation": 7, "admitted_at_ns": 1000,
            "lease_token": TOKEN, "sending_pid": 123, "lease_deadline": time.time() + 1.5,
            "delivery_status": "in_flight", "delivery_attempts": 0, "resync_generation": 0,
            "last_source_timestamp": 50.0,
        }
        self.panes = self.state_dir / "panes"
        self.hex = get_hex_pane_id(PANE)

    def session(self):
        return self.data["sessions"].get(self.sid_)

    def tx(self, state="Working", seq=2, token=TOKEN, resync=0):
        return Transmission(session_id=self.sid_, pane_id=PANE, state=state, seq=seq, agent="Claude (Herdr)",
                            lease_token=token, resync_generation=resync, generation=7, admitted_at_ns=1000,
                            arrival_ns=5000)

    # -- success -------------------------------------------------------------------
    def test_success_applies_delivery_and_side_effects(self):
        """§3.3 row 200 ok:true: delivered fields, counters reset, marker touched, flags cleared, vendor
        dismissal staged, exhausted sessions re-armed, lease cleared. (A success after DELIVERY_DOWN is the bridge
        reconnection and also re-syncs: tests/test_reconnect_resync.py.)"""
        touch_pane_failed(PANE)
        self.data["consecutive_failures"] = 2
        self.data["sessions"]["other"] = {"delivery_status": "retryable_exhausted", "delivery_attempts": 5}
        effects = apply_delivery_result(self.data, self.tx(), Outcome("success"))
        s = self.session()
        self.assertEqual(effects.verdict, "delivered")
        self.assertEqual((s["delivered_state"], s["delivered_seq"], s["delivery_status"]), ("Working", 2, "delivered"))
        self.assertEqual((s["delivery_attempts"], s["delivery_error"]), (0, None))
        self.assertEqual(self.data["consecutive_failures"], 0)
        self.assertTrue((self.panes / self.hex).exists())
        self.assertFalse((self.panes / f"{self.hex}.failed").exists())
        self.assertFalse(is_delivery_down())
        self.assertEqual([c["pane_id"] for c in self.data["pending_vendor_cleanups"]], [PANE])
        self.assertEqual(effects.vendor_cleanups[0]["is_pane_closed"], False)
        self.assertEqual(self.data["sessions"]["other"]["delivery_status"], "in_flight")
        self.assertEqual(self.data["sessions"]["other"]["delivery_attempts"], 0)
        self.assertEqual((s["lease_token"], s["sending_pid"], s["lease_deadline"]), (None, None, None))
        self.assertFalse(effects.spawn_reconciler)

    def test_success_ended_container_evicts_with_persisted_origins(self):
        """§4.3 Step C eviction: Ended + seq match evicts, removes marker, stages orphan removal and vendor cleanup,
        and re-records the tombstone from the session's persisted Step A origins."""
        s = self.session()
        s.update({"desired_state": "Ended", "close_kind": "container", "closed_at_ns": 4242,
                  "closed_source_ts": 99.5, "last_source_timestamp": 98.0})
        self.panes.mkdir(parents=True, exist_ok=True)
        (self.panes / self.hex).write_text("1")
        effects = apply_delivery_result(self.data, self.tx("Ended"), Outcome("success"))
        self.assertEqual(effects.verdict, "evicted")
        self.assertTrue(effects.evicted)
        self.assertIsNone(self.session())
        self.assertFalse((self.panes / self.hex).exists())
        self.assertEqual(effects.orphans_to_remove, (self.sid_,))
        self.assertEqual(self.data["tombstones"][PANE],
                         {"closed_at_ns": 4242, "closed_source_ts": 99.5, "last_source_timestamp": 98.0})
        self.assertNotIn(PANE, self.data["agent_exits"])
        self.assertTrue(effects.vendor_cleanups[0]["is_pane_closed"])

    def test_success_ended_agent_exit_records_agent_exits_not_tombstone(self):
        """Plan §4.1 agent_exits discipline: an agent exit records agent_exits from persisted origins, no tombstone."""
        self.session().update({"desired_state": "Ended", "close_kind": "agent_exit", "exit_at_ns": 777, "exit_source_ts": 12.5})
        effects = apply_delivery_result(self.data, self.tx("Ended"), Outcome("success"))
        self.assertTrue(effects.evicted)
        self.assertEqual(self.data["agent_exits"][PANE], {"exit_at_ns": 777, "exit_source_ts": 12.5})
        self.assertNotIn(PANE, self.data["tombstones"])
        self.assertIs(effects.vendor_cleanups[0]["is_pane_closed"], False, "an agent exit leaves the pane open (R24)")

    def test_success_ended_superseded_by_newer_turn_is_not_evicted(self):
        """§4.3 Active Supersession: a newer seq arrived; do not evict, hand the newer state on."""
        self.session().update({"desired_state": "Working", "seq": 3})
        effects = apply_delivery_result(self.data, self.tx("Ended"), Outcome("success"))
        self.assertFalse(effects.evicted)
        self.assertIsNotNone(self.session())
        self.assertEqual(self.session()["delivered_seq"], 2)
        self.assertTrue(effects.touch_pending and effects.spawn_reconciler)
        self.assertIsNone(self.session()["lease_token"])

    def test_retain_lease_refreshes_deadline_for_followup(self):
        """§4.3 'newer state arrived': a caller that keeps sending refreshes lease_deadline instead of clearing it."""
        self.session()["seq"] = 3
        effects = apply_delivery_result(self.data, self.tx(), Outcome("success"), retain_lease=True, now=100.0)
        self.assertTrue(effects.followup)
        self.assertEqual(self.session()["lease_token"], TOKEN)
        self.assertEqual(self.session()["lease_deadline"], 101.5)
        self.assertFalse(effects.spawn_reconciler)

    def test_retain_lease_with_failed_outcome_clears_lease_and_hands_off(self):
        """§4.3 'newer state arrived' (finding d7): a follow-up send is only kept after a success; a failed
        transmission clears the caller's lease and hands the newer seq to the reconciler."""
        self.session()["seq"] = 3
        effects = apply_delivery_result(self.data, self.tx(), Outcome("retryable", "network_timeout"),
                                        retain_lease=True, now=100.0)
        self.assertFalse(effects.followup)
        self.assertIsNone(self.session()["lease_token"])
        self.assertTrue(effects.spawn_reconciler and effects.touch_pending)

    def test_resync_generation_bump_forces_resync(self):
        """§4.3 Step C success: a resync generation newer than the snapshot forces delivered_seq = 0."""
        self.session()["resync_generation"] = 1
        effects = apply_delivery_result(self.data, self.tx(), Outcome("success"))
        self.assertEqual(effects.verdict, "resync")
        self.assertEqual((self.session()["delivered_seq"], self.session()["delivery_status"]), (0, "in_flight"))
        self.assertTrue(effects.touch_pending and effects.spawn_reconciler)

    def test_lease_superseded_forces_resync_and_keeps_new_lease(self):
        """§4.3 Step C lease check: another token -> bump resync_generation, delivered_seq = 0, keep their lease."""
        effects = apply_delivery_result(self.data, self.tx(token="other-token"), Outcome("success"))
        s = self.session()
        self.assertEqual(effects.verdict, "superseded")
        self.assertEqual((s["resync_generation"], s["delivered_seq"], s["delivery_status"]), (1, 0, "in_flight"))
        self.assertEqual(s["lease_token"], TOKEN)
        self.assertTrue(effects.touch_pending and effects.spawn_reconciler)

    def test_stale_result_is_ignored(self):
        """§4.3 L483 staleness check: transmitting_seq below delivered_seq changes nothing."""
        self.session()["delivered_seq"] = 3
        before = dict(self.session())
        effects = apply_delivery_result(self.data, self.tx(seq=2), Outcome("retryable", "network_timeout"))
        self.assertEqual(effects.verdict, "stale")
        self.assertEqual(self.session(), before)

    def test_failure_for_an_already_confirmed_seq_is_stale(self):
        """§4.3 L483 staleness (finding d1): a late failure for a seq another sender already confirmed
        (transmitting_seq == delivered_seq) changes nothing: no attempt, no .failed, no failure streak."""
        self.session().update({"delivered_seq": 2, "delivered_state": "Working"})
        self.data["consecutive_failures"] = 1
        for status in ("retryable", "non_retryable"):
            with self.subTest(status=status):
                before = dict(self.session())
                effects = apply_delivery_result(self.data, self.tx(seq=2), Outcome(status, "network_timeout"))
                self.assertEqual(effects.verdict, "stale")
                self.assertEqual(self.session(), before)
                self.assertEqual(self.data["consecutive_failures"], 1)
                self.assertFalse((self.panes / f"{self.hex}.failed").exists())

    def test_late_send_of_a_taken_over_lease_forces_resync(self):
        """Plan §1 row 6 + §4.3 Step C: a hung sender's older seq (its lease taken over, a newer seq delivered by the
        new holder) may reach Bartender AFTER the newer one, and Bartender renders the last state it received. A
        success or a retryable outcome (may have landed) must force the re-sync instead of being dropped as stale."""
        for status in ("success", "retryable"):
            with self.subTest(status=status):
                self.session().update({"lease_token": None, "sending_pid": None, "lease_deadline": None,
                                       "seq": 3, "delivered_seq": 3, "delivered_state": "Idle",
                                       "delivery_status": "delivered", "resync_generation": 0})
                effects = apply_delivery_result(self.data, self.tx(seq=2), Outcome(status, "network_timeout"))
                s = self.session()
                self.assertEqual(effects.verdict, "superseded")
                self.assertEqual((s["resync_generation"], s["delivered_seq"], s["delivery_status"]), (1, 0, "in_flight"))
                self.assertTrue(effects.touch_pending and effects.spawn_reconciler)

    def test_rejected_late_send_of_a_taken_over_lease_is_stale(self):
        """A non_retryable rejection proves the older send never landed: nothing to re-sync."""
        self.session().update({"lease_token": None, "seq": 3, "delivered_seq": 3, "delivered_state": "Idle",
                               "delivery_status": "delivered"})
        before = dict(self.session())
        effects = apply_delivery_result(self.data, self.tx(seq=2), Outcome("non_retryable", "bridge_rejected"))
        self.assertEqual(effects.verdict, "stale")
        self.assertEqual(self.session(), before)

    def test_duplicate_success_is_idempotent(self):
        """Finding (re-applied result): applying the same success twice (a results/ envelope re-drained after a
        crash between save and unlink, or a late result after the lease was cleared) is stale, not a forced
        re-sync of an already delivered session."""
        first = apply_delivery_result(self.data, self.tx(), Outcome("success"))
        self.assertEqual(first.verdict, "delivered")
        after_first = dict(self.session())
        again = apply_delivery_result(self.data, self.tx(), Outcome("success"))
        self.assertEqual(again.verdict, "stale")
        self.assertEqual(self.session(), after_first)
        self.assertEqual((self.session()["delivered_seq"], self.session().get("resync_generation")), (2, 0))

    # -- failures ------------------------------------------------------------------
    def test_non_retryable_rejection(self):
        """§3.3 rows 200 ok:false/3xx/4xx: non_retryable_failed, rejected_seq, error code, .failed, marker removed."""
        self.panes.mkdir(parents=True, exist_ok=True)
        (self.panes / self.hex).write_text("1")
        effects = apply_delivery_result(self.data, self.tx(), Outcome("non_retryable", "4xx_client_error"))
        s = self.session()
        self.assertEqual(effects.verdict, "rejected")
        self.assertEqual((s["delivery_status"], s["rejected_seq"], s["delivery_error"]),
                         ("non_retryable_failed", 2, "4xx_client_error"))
        self.assertTrue((self.panes / f"{self.hex}.failed").exists())
        self.assertFalse((self.panes / self.hex).exists())
        self.assertEqual(s["delivered_seq"], 1)
        self.assertFalse(effects.spawn_reconciler)
        self.assertEqual(effects.orphans_to_export, ())

    def test_non_retryable_ended_is_retained_and_orphaned(self):
        """§3.3 Zero-Data-Loss: a rejected Ended stays cached, orphaned_ended, and is staged for orphan export."""
        self.session()["desired_state"] = "Ended"
        effects = apply_delivery_result(self.data, self.tx("Ended"), Outcome("non_retryable", "bridge_rejected"))
        self.assertIs(self.session()["orphaned_ended"], True)
        self.assertEqual([sid for sid, _ in effects.orphans_to_export], [self.sid_])

    def test_retryable_failure_counts_and_delivery_down(self):
        """§3.3 rows 5xx/network: attempts++, error code, .failed on first failure, DELIVERY_DOWN at 3 consecutive
        (staged, set by the caller once the failure count is saved)."""
        self.data["consecutive_failures"] = 2
        effects = apply_delivery_result(self.data, self.tx(), Outcome("retryable", "5xx_server_error"))
        s = self.session()
        self.assertEqual(effects.verdict, "retry")
        self.assertEqual((s["delivery_attempts"], s["delivery_error"]), (1, "5xx_server_error"))
        self.assertTrue((self.panes / f"{self.hex}.failed").exists())
        self.assertEqual(self.data["consecutive_failures"], 3)
        self.assertIs(effects.delivery_down, True)
        commit_delivery_down(effects)
        self.assertTrue(is_delivery_down())
        self.assertTrue(effects.spawn_reconciler)
        self.assertIsNone(s["lease_token"])

    def test_retryable_exhaustion_after_five_attempts(self):
        """§3.3: the 5th retryable failure marks retryable_exhausted; an Ended is orphaned and exported."""
        self.session().update({"delivery_attempts": 4, "desired_state": "Ended"})
        effects = apply_delivery_result(self.data, self.tx("Ended"), Outcome("retryable", "network_timeout"))
        s = self.session()
        self.assertEqual(effects.verdict, "exhausted")
        self.assertEqual((s["delivery_status"], s["delivery_attempts"]), ("retryable_exhausted", 5))
        self.assertIs(s["orphaned_ended"], True)
        self.assertEqual([sid for sid, _ in effects.orphans_to_export], [self.sid_])

    # -- compensation ----------------------------------------------------------------
    def test_missing_session_success_stages_persisted_compensation(self):
        """§4.3 Stale Send Compensation: a non-Ended success for an evicted session stages a persisted Ended
        compensation with the real agent and target generation."""
        del self.data["sessions"][self.sid_]
        effects = apply_delivery_result(self.data, self.tx(), Outcome("success"))
        self.assertEqual(effects.verdict, "compensate")
        (comp,) = self.data["pending_compensations"]
        self.assertEqual((comp["session_id"], comp["pane_id"], comp["agent"], comp["generation"], comp["admitted_at_ns"]),
                         (self.sid_, PANE, "Claude (Herdr)", 7, 1000))
        self.assertEqual(effects.compensations, (comp,))
        apply_delivery_result(self.data, self.tx(), Outcome("success"))
        self.assertEqual(len(self.data["pending_compensations"]), 1, "compensations are de-duplicated per session")

    def test_restaged_compensation_keeps_the_may_have_landed_mark(self):
        """A compensation replaced for the same session while an earlier Ended may have landed keeps ``posted``, so a
        later re-admission is re-synced rather than merely spared (sender.compensation)."""
        del self.data["sessions"][self.sid_]
        self.data["pending_compensations"] = [{"session_id": self.sid_, "pane_id": PANE, "agent": "Claude (Herdr)",
                                               "generation": 6, "admitted_at_ns": 1, "timestamp": 1.0,
                                               "attempts": 2, "last_attempt": 2.0, "posted": True}]
        apply_delivery_result(self.data, self.tx(), Outcome("success"))
        (comp,) = self.data["pending_compensations"]
        self.assertEqual((comp["generation"], comp.get("posted"), comp.get("attempts")), (7, True, None))

    def test_missing_session_rejection_or_ended_needs_nothing(self):
        """§4.3: the bridge rejected the send (nothing landed) or the send was the Ended itself -> no compensation."""
        del self.data["sessions"][self.sid_]
        self.assertEqual(apply_delivery_result(self.data, self.tx(), Outcome("non_retryable")).verdict, "missing")
        self.assertEqual(apply_delivery_result(self.data, self.tx("Ended"), Outcome("success")).verdict, "missing")
        self.assertEqual(apply_delivery_result(self.data, self.tx("Ended"), Outcome("retryable")).verdict, "missing")
        self.assertEqual(self.data["pending_compensations"], [])

    def test_missing_session_retryable_outcome_still_compensates(self):
        """§4.3 Step C 'session is None' (finding: compensation narrowed to success): a retryable outcome (e.g. a
        socket timeout) may still have reached Bartender, so the phantom non-Ended state is compensated."""
        del self.data["sessions"][self.sid_]
        effects = apply_delivery_result(self.data, self.tx(), Outcome("retryable", "network_timeout"))
        self.assertEqual(effects.verdict, "compensate")
        self.assertEqual([c["session_id"] for c in self.data["pending_compensations"]], [self.sid_])

    def test_tombstoned_pane_retryable_outcome_compensates_unless_readmitted(self):
        """§4.3 Step C tombstone branch (finding: compensation narrowed to success): a retryable non-Ended send
        on a tombstoned pane is compensated (no state advancement); a rejected one needs nothing."""
        self.data["tombstones"][PANE] = {"closed_at_ns": 1, "closed_source_ts": 0.0, "last_source_timestamp": 0.0}
        self.session()["desired_state"] = "Ended"
        effects = apply_delivery_result(self.data, self.tx(), Outcome("retryable", "network_timeout"))
        self.assertEqual(effects.verdict, "compensate")
        self.assertEqual(self.session()["delivery_attempts"], 0, "no retry bookkeeping on a tombstoned pane")
        self.data["pending_compensations"] = []
        self.session()["lease_token"] = TOKEN
        effects = apply_delivery_result(self.data, self.tx(), Outcome("non_retryable", "4xx_client_error"))
        self.assertEqual(effects.verdict, "tombstoned")
        self.assertEqual(self.data["pending_compensations"], [])
        self.session().update({"desired_state": "Working", "lease_token": TOKEN})
        effects = apply_delivery_result(self.data, self.tx(), Outcome("retryable", "network_timeout"))
        self.assertEqual(effects.verdict, "compensation_aborted")
        self.assertEqual(self.data["pending_compensations"], [])

    def test_vendor_cleanup_close_request_wins(self):
        """§4.3 persisted side effects (finding d12): one entry per pane; a later non-close cleanup never
        downgrades a pending pane-closed request."""
        stage_vendor_cleanup(self.data, PANE, True, 1.0)
        stage_vendor_cleanup(self.data, PANE, False, 2.0)
        self.assertEqual(self.data["pending_vendor_cleanups"], [{"pane_id": PANE, "is_pane_closed": True, "timestamp": 2.0}])

    def test_tombstoned_pane_compensates_unless_readmitted(self):
        """§4.3 under-lock re-verification: tombstoned + success -> compensation, unless a live session was re-admitted."""
        self.data["tombstones"][PANE] = {"closed_at_ns": 1, "closed_source_ts": 0.0, "last_source_timestamp": 0.0}
        self.session()["desired_state"] = "Ended"
        effects = apply_delivery_result(self.data, self.tx(), Outcome("success"))
        self.assertEqual(effects.verdict, "compensate")
        self.assertEqual(self.session()["delivered_seq"], 1, "no state advancement on a tombstoned pane")
        self.data["pending_compensations"] = []
        self.session().update({"desired_state": "Working", "lease_token": TOKEN})
        effects = apply_delivery_result(self.data, self.tx(), Outcome("success"))
        self.assertEqual(effects.verdict, "compensation_aborted")
        self.assertEqual(self.data["pending_compensations"], [])

    def test_transmission_round_trips_through_dict(self):
        """Result envelopes carry the Transmission snapshot verbatim."""
        tx = self.tx()
        self.assertEqual(Transmission.from_dict(tx.as_dict()), tx)
        snap = Transmission.snapshot(self.sid_, self.session(), TOKEN, arrival_ns=5000)
        self.assertEqual(snap, tx)


if __name__ == "__main__":
    unittest.main()
