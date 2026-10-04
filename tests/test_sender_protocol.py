"""Universal Sender Protocol Step B / policy / hand-off, driven through the real handlers and the mock bridge.

Gaps: step-b-sync-cap, ended-retry-budget-and-timeout, minimal-retry-noop, unconditional-watchdog-check,
background-budget-starvation (policy side), and the Step B critical-section assertion; the unsent-claim, stale-outcome
and unexpected-error hand-offs.
"""

import time
import unittest
from unittest import mock

import herdr_bartender.sender as sender_pkg
from herdr_bartender import clock, runtime
from herdr_bartender.bridge import ERR_INVALID_BRIDGE_URL, DeliveryResult
from herdr_bartender.cache import LockTimeout
from herdr_bartender.handlers import flow, handle_agent_status_changed, handle_pane_closed
from herdr_bartender.sender import (
    BACKGROUND_POLICY,
    EVENT_POLICY,
    CriticalSectionViolation,
    Sent,
    Staged,
    Target,
    claim_lease,
    deliver_claim,
    run_step_a,
    settle,
    step_b,
    step_c,
    transmit,
)
from tests.support import SandboxTestCase

PANE = "w1:pSend"


def _working(pane=PANE, status="working", **extra):
    return {"agent_status": status, "pane_id": pane, "workspace_id": pane.split(":")[0], "agent": "claude", **extra}


class SenderCase(SandboxTestCase):
    def _session(self, pane=PANE):
        with self.cache_mgr as data:
            return data["sessions"].get(self.sid(pane))

    def _posts_for(self, pane=PANE):
        return [body for body in self.bridge.posts() if isinstance(body, dict) and body.get("session_id") == self.sid(pane)]

    def _set_budget(self, remaining: float) -> None:
        """Make the event-path time_remaining() return about ``remaining`` seconds."""
        runtime.PROCESS_DEADLINE_SECONDS = remaining
        runtime.START_TIME = clock.monotonic()

    @property
    def pending(self):
        return self.state_dir / "reconciler.pending"


class StepBCapTests(SenderCase):
    def test_newer_seq_during_send_hands_off_instead_of_looping(self):
        """R5 (gap step-b-sync-cap): a newer seq staged while the POST is in flight is NOT sent by this process;
        Step C confirms the transmitted seq, clears the lease, flags the reconciler and ensures it once."""
        def newer_seq_arrives(payload):
            if payload.get("seq") != 1:
                return
            with self.cache_mgr as data:  # another process's Step A deferring to our live lease
                s = data["sessions"][self.sid(PANE)]
                s.update({"seq": 2, "desired_state": "Waiting",
                          "desired_payload": {**s["desired_payload"], "state": "Waiting", "seq": 2}})
                self.cache_mgr.save(data)

        self.bridge.on_post = newer_seq_arrives
        handle_agent_status_changed(_working(), {}, bridge_url=self.mock_url)
        self.assertEqual([(p["state"], p["seq"]) for p in self._posts_for()], [("Working", 1)])
        s = self._session()
        self.assertEqual((s["delivered_seq"], s["seq"]), (1, 2))
        self.assertEqual((s["lease_token"], s["sending_pid"], s["lease_deadline"]), (None, None, None))
        self.assertTrue(self.pending.exists())
        self.assertEqual(len(self.spawner.calls), 1, "a single hand-off per process")

    def test_rejected_ended_is_posted_at_most_twice(self):
        """R5: primary POST + one minimal retry, never a third POST, then non_retryable bookkeeping."""
        handle_agent_status_changed(_working(), {}, bridge_url=self.mock_url)
        for _ in range(3):
            self.bridge.enqueue(400, b'{"ok":false}')
        handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url)
        ended = [p for p in self._posts_for() if p["state"] == "Ended"]
        self.assertEqual(len(ended), 2)
        s = self._session()
        self.assertEqual((s["delivery_status"], s["delivery_error"], s["orphaned_ended"]),
                         ("non_retryable_failed", "4xx_client_error", True))


class MinimalRetryTests(SenderCase):
    def setUp(self):
        super().setUp()
        handle_agent_status_changed(_working(), {}, bridge_url=self.mock_url)

    def test_rejected_full_ended_is_retried_with_literal_minimal_payload(self):
        """Plan §3.3 / R19 (gap minimal-retry-noop): the retry is exactly {state, agent: "Herdr", session_id}."""
        self.bridge.reject_complex_ended = True
        handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url)
        first, retry = [p for p in self._posts_for() if p["state"] == "Ended"]
        self.assertTrue({"terminal", "event", "seq", "title", "cwd"} <= set(first))
        self.assertEqual(retry, {"state": "Ended", "agent": "Herdr", "session_id": self.sid(PANE)})
        self.assertIsNone(self._session(), "the accepted minimal retry confirms the Ended (evicted)")

    def test_retryable_ended_failure_is_not_retried_inline(self):
        """Narrow §3.3 reading: a 5xx / network failure on Ended is left to the reconciler, not retried inline."""
        self.bridge.enqueue(500)
        handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url)
        self.assertEqual(len([p for p in self._posts_for() if p["state"] == "Ended"]), 1)
        self.assertEqual(self._session()["delivery_error"], "5xx_server_error")

    def test_rejected_non_ended_is_never_retried(self):
        self.bridge.enqueue(400)
        handle_agent_status_changed(_working(status="blocked"), {}, bridge_url=self.mock_url)
        self.assertEqual([p["state"] for p in self._posts_for()], ["Working", "Waiting"])

    def test_invalid_bridge_url_is_not_retried(self):
        """A configuration error is not a bridge rejection: no minimal retry (one send for the claim)."""
        with mock.patch.object(step_b, "send_event", wraps=step_b.send_event) as sent:
            handle_pane_closed({"pane_id": PANE}, {}, bridge_url="http://example.com:9")
        self.assertEqual(sent.call_count, 1)
        self.assertEqual(self._session()["delivery_error"], ERR_INVALID_BRIDGE_URL)

    def test_minimal_retry_needs_budget(self):
        """Plan §4.3 L479 (gap ended-retry-budget-and-timeout): no retry once time_remaining() <= 0.3s."""
        self.bridge.on_post = lambda payload: self._set_budget(0.2) if payload.get("state") == "Ended" else None
        self.bridge.enqueue(400)
        handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url)
        self.assertEqual(len([p for p in self._posts_for() if p["state"] == "Ended"]), 1)
        self.assertEqual(self._session()["delivery_status"], "non_retryable_failed")


class BudgetGateTests(SenderCase):
    def test_no_post_without_budget_and_lease_released(self):
        """Plan §4.3 L472 / §6.1: the primary POST needs time_remaining() > 0.3s; otherwise the lease is released
        and the staged seq is handed to the reconciler."""
        self._set_budget(0.29)
        handle_agent_status_changed(_working(), {}, bridge_url=self.mock_url)
        self.assertEqual(self.bridge.requests, [])
        s = self._session()
        self.assertEqual((s["delivery_status"], s.get("delivered_seq", 0), s["seq"]), ("in_flight", 0, 1))
        self.assertEqual((s["lease_token"], s["sending_pid"]), (None, None))
        self.assertTrue(self.pending.exists())
        self.assertEqual(len(self.spawner.calls), 1)

    def test_budget_gate_needs_more_than_the_reserve(self):
        """Plan §6.1: network I/O needs MORE than 0.3s left; exactly 0.3s (frozen clock) is not enough."""
        self.use_fake_clock()
        for remaining, allowed in ((0.3, False), (0.31, True)):
            with self.subTest(remaining=remaining):
                self._set_budget(remaining)
                self.assertEqual(runtime.time_remaining(), remaining)
                self.assertIs(EVENT_POLICY.allows_network(), allowed)

    def test_unsent_claim_whose_step_c_cannot_lock_hands_off(self):
        """Step B sent nothing (budget) and Step C cannot lock: the lease lapses, and the owed Ended (no live session
        left, so no watchdog check would cover it) is still flagged and the reconciler ensured."""
        handle_agent_status_changed(_working(), {}, bridge_url=self.mock_url)
        self.spawner.reset()
        self._set_budget(0.29)
        with mock.patch.object(step_c, "_settle_locked", side_effect=LockTimeout("contended")):
            handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url)
        self.assertEqual([p["state"] for p in self._posts_for()], ["Working"])
        self.assertTrue(self.pending.exists())
        self.assertEqual(len(self.spawner.calls), 1)

    def test_unsent_step_c_keeps_a_lease_taken_over_meanwhile(self):
        """Step C of an unsent claim releases only its own lease, never one another sender took in the meantime."""
        foreign = "4242:None:1.0:other"

        def taken_over_while_unsent(claim, policy, bridge_url=None):
            with self.cache_mgr as data:
                data["sessions"][claim.session_id].update(
                    {"lease_token": foreign, "sending_pid": 4242, "lease_deadline": time.time() + 1.5})
                self.cache_mgr.save(data)
            return Sent(None, 0, step_b.NOT_SENT_BUDGET)

        with mock.patch.object(sender_pkg, "transmit", side_effect=taken_over_while_unsent):
            handle_agent_status_changed(_working(), {}, bridge_url=self.mock_url)
        s = self._session()
        self.assertEqual((s["lease_token"], s["sending_pid"]), (foreign, 4242))

    def test_socket_timeout_formula(self):
        """Plan §4.3 L478: timeout = min(0.2, max(0.05, time_remaining() - 0.3))."""
        cases = {10.0: 0.2, 0.45: 0.15, 0.36: 0.06, 0.32: 0.05}
        for remaining, expected in cases.items():
            with self.subTest(remaining=remaining):
                self._set_budget(remaining)
                self.assertAlmostEqual(EVENT_POLICY.socket_timeout(), expected, delta=0.01)

    def test_handler_posts_with_the_policy_timeout(self):
        seen = []

        def send(payload, timeout=None, bridge_url=None):
            seen.append(timeout)
            return DeliveryResult("success", None, 200)

        self._set_budget(0.45)
        with mock.patch.object(step_b, "send_event", side_effect=send):
            handle_agent_status_changed(_working(), {}, bridge_url=self.mock_url)
        self.assertEqual(len(seen), 1)
        self.assertLessEqual(seen[0], 0.15)
        self.assertGreaterEqual(seen[0], 0.05)


class CriticalSectionTests(SenderCase):
    def test_step_b_refuses_to_send_under_the_cache_lock(self):
        """Plan §1 L9: ``assert not IN_CRITICAL_SECTION`` holds in every mode (not only under tests)."""
        handle_agent_status_changed(_working(), {}, bridge_url=self.mock_url)
        before = len(self.bridge.requests)
        with self.cache_mgr as data:
            claim = claim_lease(self.sid(PANE), PANE, data["sessions"][self.sid(PANE)], time.time(), 1)
            with self.assertRaises(CriticalSectionViolation):
                transmit(claim, EVENT_POLICY, self.mock_url)
        self.assertEqual(len(self.bridge.requests), before)


class WatchdogCheckTests(SenderCase):
    """Plan §4.3 L568 (gap unconditional-watchdog-check)."""

    def test_event_leaving_a_live_session_ensures_the_reconciler(self):
        handle_agent_status_changed(_working(), {}, bridge_url=self.mock_url)
        self.assertEqual(len(self.spawner.calls), 1)
        self.assertFalse(self.pending.exists(), "nothing is owed: a plain watchdog ensure, no forced pass")

    def test_dropped_event_still_checks_the_watchdog(self):
        handle_agent_status_changed(_working(timestamp=1000.0), {}, bridge_url=self.mock_url)
        self.spawner.reset()
        handle_agent_status_changed(_working(status="blocked", timestamp=10.0), {}, bridge_url=self.mock_url)
        self.assertEqual(self._session()["desired_state"], "Working", "stale event dropped")
        self.assertEqual(len(self.spawner.calls), 1)

    def test_event_rejected_at_intake_still_checks_the_watchdog(self):
        """L568 is unconditional: an event ignored before Step A (unrecognized status, no identity) still makes sure
        the reconciler runs while a live session is cached."""
        handle_agent_status_changed(_working(), {}, bridge_url=self.mock_url)
        for event in (_working(status="definitely-not-a-status"), {"agent_status": "working"}):
            with self.subTest(event=event):
                self.spawner.reset()
                handle_agent_status_changed(event, {}, bridge_url=self.mock_url)
                self.assertEqual(len(self.spawner.calls), 1)
        self.assertEqual(len(self._posts_for()), 1)

    def test_event_rejected_at_intake_without_live_sessions_spawns_nothing(self):
        handle_agent_status_changed(_working(status="definitely-not-a-status"), {}, bridge_url=self.mock_url)
        self.assertEqual(self.spawner.calls, [])

    def test_closing_the_last_session_needs_no_watchdog(self):
        handle_agent_status_changed(_working(), {}, bridge_url=self.mock_url)
        self.spawner.reset()
        handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url)
        self.assertIsNone(self._session())
        self.assertEqual(self.spawner.calls, [])


class StepCOutcomeTests(SenderCase):
    def _claim_delivered_session(self):
        handle_agent_status_changed(_working(), {}, bridge_url=self.mock_url)
        sid = self.sid(PANE)
        cache_mgr = EVENT_POLICY.cache(self.state_dir)
        stage = lambda data: Staged((Target(sid, PANE, data["sessions"][sid]),), mutated=True)  # noqa: E731
        (claim,) = run_step_a(cache_mgr, stage, policy=EVENT_POLICY, arrival_ns=1).claims
        return cache_mgr, claim

    def test_stale_outcome_releases_our_own_lease(self):
        """A Step C verdict that changes nothing else (STALE: this seq was confirmed meanwhile) still releases the
        claimant's own lease, so other senders never defer to a long-lived holder (the reconciler) for nothing."""
        cache_mgr, claim = self._claim_delivered_session()
        self.assertEqual(self._session()["lease_token"], claim.token)
        post = settle(cache_mgr, claim, Sent(DeliveryResult("success", None, 200), 1), EVENT_POLICY)
        s = self._session()
        self.assertEqual((s["lease_token"], s["sending_pid"], s["lease_deadline"]), (None, None, None))
        self.assertFalse(post.hand_off, "nothing is owed")

    def test_unexpected_error_after_step_a_still_hands_off(self):
        """A bug after Step A saved and claimed degrades to a reconciler retry: logged, handed off, re-raised."""
        with mock.patch.object(flow, "deliver_claim", side_effect=RuntimeError("bug")):
            with self.assertRaises(RuntimeError):
                handle_agent_status_changed(_working(), {}, bridge_url=self.mock_url)
        self.assertEqual(self._session()["seq"], 1, "Step A saved the event")
        self.assertTrue(self.pending.exists())
        self.assertEqual(len(self.spawner.calls), 1)


class BackgroundPolicyTests(SenderCase):
    """Gap background-budget-starvation (policy side): the reconciler reuses the sender without the event deadline."""

    def _stage_all(self, panes):
        def stage(data):
            return Staged(tuple(Target(self.sid(p), p, data["sessions"][self.sid(p)]) for p in panes), mutated=True)
        return stage

    def _seed_undelivered(self, panes):
        with self.cache_mgr as data:
            for pane in panes:
                sid = self.sid(pane)
                data["sessions"][sid] = {
                    "pane_id": pane, "desired_state": "Working", "seq": 1, "delivered_seq": 0, "generation": 1,
                    "delivery_status": "in_flight", "agent": "Claude (Herdr)", "last_event_at": time.time(),
                    "desired_payload": {"state": "Working", "agent": "Claude (Herdr)", "session_id": sid, "seq": 1},
                }
            self.cache_mgr.save(data)

    def test_background_policy_sends_when_the_event_budget_is_spent(self):
        panes = ["w1:pBg1", "w1:pBg2", "w1:pBg3"]
        self._seed_undelivered(panes)
        self._set_budget(0.0)
        runtime.set_deadline_mode(runtime.DEADLINE_UNBOUNDED)  # what the reconciler process declares
        self.assertFalse(EVENT_POLICY.allows_network())
        self.assertTrue(BACKGROUND_POLICY.allows_network())
        self.assertEqual(BACKGROUND_POLICY.socket_timeout(), 0.2)
        cache_mgr = BACKGROUND_POLICY.cache(self.state_dir)
        result = run_step_a(cache_mgr, self._stage_all(panes), policy=BACKGROUND_POLICY, arrival_ns=1)
        self.assertEqual(len(result.claims), 3, "no one-session clamp outside the event path")
        for claim in result.claims:
            report = deliver_claim(cache_mgr, claim, policy=BACKGROUND_POLICY, bridge_url=self.mock_url)
            self.assertFalse(report.hand_off)
        for pane in panes:
            self.assertEqual(self._session(pane)["delivery_status"], "delivered")

    def test_event_policy_clamps_and_background_hand_off_never_spawns(self):
        panes = ["w1:pBg1", "w1:pBg2"]
        self._seed_undelivered(panes)
        cache_mgr = EVENT_POLICY.cache(self.state_dir)
        result = run_step_a(cache_mgr, self._stage_all(panes), policy=EVENT_POLICY, arrival_ns=1)
        self.assertEqual((len(result.claims), result.hand_off), (1, True))
        BACKGROUND_POLICY.hand_off()
        self.assertTrue(self.pending.exists())
        self.assertEqual(self.spawner.calls, [], "the reconciler never spawns itself")
        self.assertTrue(BACKGROUND_POLICY.orphan_blocking and not EVENT_POLICY.orphan_blocking)


if __name__ == "__main__":
    unittest.main()
