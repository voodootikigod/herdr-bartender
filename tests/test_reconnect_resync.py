"""A bridge reconnection after DELIVERY_DOWN is a Full Top Shelf Re-Sync, whoever sees it first (Plan §1 L113, §5.1 item 4).

The first confirmed contact after DELIVERY_DOWN may be an event-path Step C, a deferred ``results/`` envelope applied by
the reconciler's drain, or the reconciler's own sweep - not only its ``/health`` probe. Each of them must re-assert
every other live, non-salvaged session in the same critical section that clears DELIVERY_DOWN.
"""

import unittest
from unittest import mock

from herdr_bartender import cache, process
from herdr_bartender.background import run_reconcile_background
from herdr_bartender.cache import BoundedSessionCache, CacheWriteError
from herdr_bartender.delivery_state import Outcome, Transmission, apply_delivery_result, commit_delivery_down
from herdr_bartender.handlers import handle_agent_status_changed
from herdr_bartender.markers import is_delivery_down, touch_delivery_down
from herdr_bartender.reconciler import reconcile_active_sessions
from herdr_bartender.replay import settle_confirmed
from herdr_bartender.results import drain_results_dir, write_result_envelope
from tests.support import SandboxTestCase
from tests.support.reconciler_fixtures import read_cache, salvaged, seed, session
from tests.support.sandbox import DEFAULT_BARTENDER_PID, DEFAULT_LSTART

TOKEN = "4242:1:1.0:x"


class ReconnectResyncCase(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.clock = self.use_fake_clock()
        now = self.clock.time()
        self.a, self.b, self.quiet = self.sid("w1:pA"), self.sid("w1:pB"), self.sid("w1:pQuiet")
        seed(self.cache_mgr, {self.a: session("w1:pA", "Working", seq=3, now=now),
                              self.quiet: salvaged("w1:pQuiet", now=now)},
             last_bartender_pid=DEFAULT_BARTENDER_PID,
             last_bartender_start_time=process.parse_lstart(DEFAULT_LSTART))
        touch_delivery_down()

    def sessions(self):
        return read_cache(self.cache_mgr)["sessions"]

    def assert_a_resynced_then_resent(self):
        record = self.sessions()[self.a]
        self.assertEqual((record["delivered_seq"], record["delivery_status"], record["resync_generation"]),
                         (0, "in_flight", 1), "A was re-armed for a re-sync in the reconnection's critical section")
        self.assertFalse(is_delivery_down())
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        self.assertEqual([(e["state"], e["seq"]) for e in self.bridge.events_for(self.a)], [("Working", 3)])
        self.assertEqual(self.sessions()[self.a]["delivery_status"], "delivered")
        self.assertEqual(self.bridge.events_for(self.quiet), [], "salvaged sessions stay quiescent")


class ReconnectResyncTests(ReconnectResyncCase):
    def test_event_path_success_after_delivery_down_resyncs_the_other_live_sessions(self):
        handle_agent_status_changed({"agent_status": "working", "pane_id": "w1:pB", "workspace_id": "w1",
                                     "agent": "claude"}, {}, bridge_url=self.mock_url)
        self.assertEqual(self.sessions()[self.b]["delivery_status"], "delivered")
        self.assertTrue((self.state_dir / "reconciler.pending").exists(), "the re-sync is handed to the reconciler")
        self.assertEqual(self.bridge.events_for(self.a), [])
        self.assert_a_resynced_then_resent()

    def test_deferred_result_envelope_success_after_delivery_down_resyncs(self):
        """The reconciler's results drain runs before its health check: the drained success is the reconnection."""
        seed(self.cache_mgr, {self.b: session("w1:pB", seq=2, delivered=False, now=self.clock.time(),
                                              lease_token=TOKEN, sending_pid=4242,
                                              lease_deadline=self.clock.time() - 10)})
        record = read_cache(self.cache_mgr)["sessions"][self.b]
        write_result_envelope(Transmission.snapshot(self.b, record, TOKEN), Outcome("success"))
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        self.assertEqual(self.bridge.events_for(self.b), [], "B's own success came from the envelope")
        self.assertEqual([(e["state"], e["seq"]) for e in self.bridge.events_for(self.a)], [("Working", 3)])
        self.assertFalse(is_delivery_down())

    def test_reconciler_sweep_success_after_delivery_down_resyncs(self):
        seed(self.cache_mgr, {self.b: session("w1:pB", seq=2, delivered=False, now=self.clock.time())})
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertEqual(len(self.bridge.events_for(self.b)), 1)
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertEqual([e["state"] for e in self.bridge.events_for(self.a)], ["Working"])
        self.assertEqual(len(self.bridge.events_for(self.b)), 1, "the confirmed session itself is not re-sent")


class ResyncSurvivesFailedSaveTests(ReconnectResyncCase):
    """DELIVERY_DOWN is cleared only once the reconnection's re-sync is saved: a confirmation whose save fails leaves
    the flag set, so whoever applies it again (results drain, retried replay, /health recovery) still re-syncs."""

    def seed_in_flight_b(self):
        seed(self.cache_mgr, {self.b: session("w1:pB", seq=2, delivered=False, now=self.clock.time(),
                                              lease_token=TOKEN, sending_pid=4242,
                                              lease_deadline=self.clock.time() - 10)})
        record = read_cache(self.cache_mgr)["sessions"][self.b]
        return write_result_envelope(Transmission.snapshot(self.b, record, TOKEN), Outcome("success"))

    def assert_unsaved_reconnection_kept(self):
        self.assertTrue(is_delivery_down(), "the re-sync was not saved, so DELIVERY_DOWN must survive")
        self.assertEqual((self.sessions()[self.a]["delivered_seq"], self.sessions()[self.a]["delivery_status"]),
                         (3, "delivered"))

    def test_step_c_save_failure_keeps_delivery_down_for_the_deferred_result(self):
        real, calls = cache.write_cache_file, []

        def fail_step_c_save(path, data):
            calls.append(path)
            if len(calls) == 2:   # Step A saves first; the second save is Step C's
                raise CacheWriteError("disk full")
            return real(path, data)

        with mock.patch.object(cache, "write_cache_file", side_effect=fail_step_c_save):
            handle_agent_status_changed({"agent_status": "working", "pane_id": "w1:pB", "workspace_id": "w1",
                                         "agent": "claude"}, {}, bridge_url=self.mock_url)
        self.assertEqual(len(self.bridge.events_for(self.b)), 1, "B's POST was confirmed")
        self.assertEqual(len(list((self.state_dir / "results").glob("*.json"))), 1, "Step C deferred to results/")
        self.assert_unsaved_reconnection_kept()
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        self.assertEqual([(e["state"], e["seq"]) for e in self.bridge.events_for(self.a)], [("Working", 3)])
        self.assertFalse(is_delivery_down())

    def test_drain_save_failure_keeps_delivery_down_for_the_next_drain(self):
        envelope = self.seed_in_flight_b()
        with mock.patch.object(BoundedSessionCache, "save", side_effect=CacheWriteError("disk full")):
            with self.assertRaises(CacheWriteError):
                drain_results_dir(self.state_dir)
        self.assertTrue(envelope.exists())
        self.assert_unsaved_reconnection_kept()
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        self.assertEqual(self.bridge.events_for(self.b), [], "B's own success came from the envelope")
        self.assertEqual([(e["state"], e["seq"]) for e in self.bridge.events_for(self.a)], [("Working", 3)])
        self.assertFalse(is_delivery_down())

    def test_replay_settle_save_failure_keeps_delivery_down_for_the_retry(self):
        ended = self.sid("w1:pE")
        seed(self.cache_mgr, {ended: session("w1:pE", "Ended", seq=2, delivered=False, now=self.clock.time())})
        with mock.patch.object(BoundedSessionCache, "save", side_effect=CacheWriteError("disk full")):
            with self.assertRaises(CacheWriteError):
                settle_confirmed(self.cache_mgr, ended, "w1:pE")
        self.assert_unsaved_reconnection_kept()
        self.assertTrue(settle_confirmed(self.cache_mgr, ended, "w1:pE"), "the retried confirmation re-syncs")
        record = self.sessions()[self.a]
        self.assertEqual((record["delivered_seq"], record["delivery_status"], record["resync_generation"]),
                         (0, "in_flight", 1))
        self.assertNotIn(ended, self.sessions())
        self.assertFalse(is_delivery_down())


class DrainBatchDeliveryDownTests(ReconnectResyncCase):
    """One drain batch is one critical section: the DELIVERY_DOWN transitions of its results are applied in order."""

    def envelope(self, sid, pane, outcome, seq=2):
        write_result_envelope(Transmission(sid, pane, "Working", seq, "Claude (Herdr)"), outcome)

    def test_second_success_in_a_batch_does_not_re_sync_again(self):
        c = self.sid("w1:pC")
        seed(self.cache_mgr, {self.b: session("w1:pB", seq=2, delivered=False, now=self.clock.time()),
                              c: session("w1:pC", seq=2, delivered=False, now=self.clock.time())})
        self.envelope(self.b, "w1:pB", Outcome("success"))
        self.envelope(c, "w1:pC", Outcome("success"))
        drain_results_dir(self.state_dir)
        sessions = self.sessions()
        self.assertEqual(sessions[self.a]["resync_generation"], 1, "one reconnection, one re-sync")
        self.assertEqual((sessions[self.b]["delivered_seq"], sessions[self.b]["delivery_status"]), (2, "delivered"),
                         "the batch's first confirmation is not re-armed by the second")
        self.assertFalse(is_delivery_down())

    def test_failures_after_the_reconnection_in_a_batch_leave_delivery_down_set(self):
        c = self.sid("w1:pC")
        seed(self.cache_mgr, {self.b: session("w1:pB", seq=2, delivered=False, now=self.clock.time()),
                              c: session("w1:pC", seq=2, delivered=False, now=self.clock.time())})
        self.envelope(self.b, "w1:pB", Outcome("success"))
        for _ in range(3):
            self.envelope(c, "w1:pC", Outcome("retryable", "network_timeout"))
        drain_results_dir(self.state_dir)
        self.assertEqual(read_cache(self.cache_mgr)["consecutive_failures"], 3)
        self.assertTrue(is_delivery_down(), "the last transition in the batch (3 failures) wins")


class ApplyResultResyncTests(SandboxTestCase):
    start_bridge = False

    def test_success_without_delivery_down_does_not_resync(self):
        data = {"sessions": {"a": {"desired_state": "Working", "seq": 2, "delivered_seq": 2,
                                   "delivery_status": "delivered", "pane_id": "w1:pA"},
                             "b": {"desired_state": "Working", "seq": 1, "delivered_seq": 0,
                                   "delivery_status": "in_flight", "pane_id": "w1:pB"}}}
        effects = apply_delivery_result(data, Transmission("b", "w1:pB", "Working", 1), Outcome("success"))
        self.assertEqual(data["sessions"]["a"]["delivered_seq"], 2)
        self.assertFalse(effects.spawn_reconciler)

    def test_success_with_delivery_down_resyncs_and_hands_off(self):
        touch_delivery_down()
        data = {"sessions": {"a": {"desired_state": "Waiting", "seq": 2, "delivered_seq": 2,
                                   "delivery_status": "delivered", "pane_id": "w1:pA"},
                             "gone": {"desired_state": "Ended", "seq": 4, "delivered_seq": 3,
                                      "delivery_status": "retryable_exhausted", "pane_id": "w1:pG"},
                             "b": {"desired_state": "Working", "seq": 1, "delivered_seq": 0,
                                   "delivery_status": "in_flight", "pane_id": "w1:pB"}}}
        effects = apply_delivery_result(data, Transmission("b", "w1:pB", "Working", 1), Outcome("success"))
        a, b = data["sessions"]["a"], data["sessions"]["b"]
        self.assertEqual((a["delivered_seq"], a["delivery_status"], a["resync_generation"]), (0, "in_flight", 1))
        self.assertEqual((b["delivered_seq"], b["delivery_status"]), (1, "delivered"))
        self.assertEqual(data["sessions"]["gone"]["delivered_seq"], 3, "an Ended is not re-synced (only re-armed)")
        self.assertTrue(effects.touch_pending and effects.spawn_reconciler)
        self.assertIs(effects.delivery_down, False, "the reconnection owes a DELIVERY_DOWN clear ...")
        self.assertTrue(is_delivery_down(), "... applied by the caller only once the re-sync is saved")
        commit_delivery_down(effects)
        self.assertFalse(is_delivery_down())

    def test_retryable_threshold_stages_delivery_down(self):
        data = {"consecutive_failures": 2,
                "sessions": {"b": {"desired_state": "Working", "seq": 1, "delivered_seq": 0,
                                   "delivery_status": "in_flight", "pane_id": "w1:pB"}}}
        effects = apply_delivery_result(data, Transmission("b", "w1:pB", "Working", 1), Outcome("retryable"))
        self.assertIs(effects.delivery_down, True)
        self.assertFalse(is_delivery_down(), "the flag follows the saved failure count")
        commit_delivery_down(effects)
        self.assertTrue(is_delivery_down())


if __name__ == "__main__":
    unittest.main()
