"""Reconciler delivery through the Universal Sender: retry schedule, lease discipline, background budget.

Gaps: reconciler/retry-schedule-missing, bridge-cli/retry-schedule, reconciler/bg-deadline-clamp,
dispatch-protocol/background-budget-starvation, reconciler-lease-batch-claim, t20 (quiet outage recovery).
"""

import json
import unittest
from unittest import mock

from herdr_bartender import background, cache, clock, runtime
from herdr_bartender.background import run_reconcile_background
from herdr_bartender.delivery_state import RETRY_DELAYS
from herdr_bartender.markers import touch_pane_marker
from herdr_bartender.paths import get_orphan_path
from herdr_bartender.reconciler import reconcile_active_sessions
from herdr_bartender.sender import step_b
from tests.support import SandboxTestCase
from tests.support.lock_holder import hold_lock
from tests.support.reconciler_fixtures import LoopRunner, pane_file, read_cache, seed, session

PANE = "w1:pRetry"


class RetryCase(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.clock = self.use_fake_clock()
        self.session_id = self.sid(PANE)

    def record(self):
        return read_cache(self.cache_mgr)["sessions"].get(self.session_id)


class RetryScheduleTests(RetryCase):
    def test_five_attempts_at_0_1_3_7_15_with_the_lease_released_between_them(self):
        """Plan §5.1 item 3 / §3.3: 5 attempts at 0/1/2/4/8s delays. Before every sleep the lease is released (no
        lease_token while sleeping) and each attempt re-claims it under the lock (a fresh token, verified in Step C).
        After the 5th failure: retryable_exhausted, marker removed, .failed touched."""
        self.bridge.return_code = 500
        self.bridge.health_ok = False
        seed(self.cache_mgr, {self.session_id: session(PANE, "Waiting", seq=2, delivered=False,
                                                       now=self.clock.time())})
        touch_pane_marker(PANE)
        start, attempts, tokens, leases_while_sleeping = self.clock.time(), [], [], set()

        def on_post(payload):
            if payload.get("session_id") == self.session_id:
                attempts.append(round(self.clock.time() - start, 3))
                tokens.append(self.record()["lease_token"])

        real_sleep = clock.sleep

        def sleep(seconds):
            record = self.record()
            leases_while_sleeping.add(record.get("lease_token") if record else None)
            real_sleep(seconds)

        self.bridge.on_post = on_post
        runner = LoopRunner(self.state_dir, stop=lambda: (self.record() or {}).get("delivery_status")
                            == "retryable_exhausted")
        with mock.patch.object(clock, "sleep", side_effect=sleep):
            runner.run(bridge_url=self.mock_url)
        self.assertEqual(attempts, [0.0, 1.0, 3.0, 7.0, 15.0], "delays of 0, 1, 2, 4 and 8s")
        self.assertEqual(len(set(tokens)), 5, "every attempt claimed a fresh lease")
        self.assertEqual(leases_while_sleeping, {None}, "no lease is held across a backoff sleep")
        record = self.record()
        self.assertEqual((record["delivery_status"], record["delivery_attempts"]), ("retryable_exhausted", 5))
        self.assertFalse(pane_file(self.state_dir, PANE).exists())
        self.assertTrue(pane_file(self.state_dir, PANE, ".failed").exists())

    def test_exhausted_ended_is_orphaned_and_exported(self):
        """§5.1 item 3: an Ended that exhausts its 5 attempts is marked orphaned_ended and exported (0600)."""
        self.bridge.return_code = 500
        self.bridge.health_ok = False
        seed(self.cache_mgr, {self.session_id: session(PANE, "Ended", seq=3, delivered=False, now=self.clock.time())})
        for _ in RETRY_DELAYS:
            reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
            self.clock.advance(8.0)
        record = self.record()
        self.assertEqual((record["delivery_status"], record["orphaned_ended"]), ("retryable_exhausted", True))
        exported = json.loads(get_orphan_path().read_text())["sessions"]
        self.assertTrue(exported[self.session_id]["orphaned_ended"])

    def test_retry_not_due_is_not_sent(self):
        seed(self.cache_mgr, {self.session_id: session(PANE, delivered=False, now=self.clock.time(),
                                                       delivery_attempts=2, next_retry_at=self.clock.time() + 2.0)})
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertEqual(self.bridge.events_for(self.session_id), [])
        self.clock.advance(2.0)
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertEqual(len(self.bridge.events_for(self.session_id)), 1)

    def test_p20_quiet_outage_recovers_without_follow_up_events(self):
        """Plan §10.1 #20: the bridge fails until the session is exhausted and DELIVERY_DOWN is set; once /health is
        ok again the next pass clears DELIVERY_DOWN, re-arms and delivers it - no new event involved."""
        self.bridge.return_code = 500
        self.bridge.health_ok = False
        seed(self.cache_mgr, {self.session_id: session(PANE, "Waiting", seq=2, delivered=False,
                                                       now=self.clock.time())})
        for _ in RETRY_DELAYS:
            reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
            self.clock.advance(8.0)
        self.assertEqual(self.record()["delivery_status"], "retryable_exhausted")
        self.assertTrue((self.state_dir / "DELIVERY_DOWN").exists())
        self.bridge.return_code, self.bridge.health_ok = 200, True
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        self.assertFalse((self.state_dir / "DELIVERY_DOWN").exists())
        record = self.record()
        self.assertEqual((record["delivery_status"], record["delivered_state"]), ("delivered", "Waiting"))
        self.assertEqual(self.bridge.sessions[self.session_id]["state"], "Waiting")

    def test_healthy_probe_rearms_exhausted_sessions_without_delivery_down(self):
        """Plan §5.1 item 3: a retryable_exhausted session is re-armed by /health ok alone (no DELIVERY_DOWN, no other
        successful POST) and delivered in the same pass."""
        seed(self.cache_mgr, {self.session_id: session(PANE, "Waiting", seq=2, delivered=False, now=self.clock.time(),
                                                       delivery_status="retryable_exhausted", delivery_attempts=5)})
        self.assertFalse((self.state_dir / "DELIVERY_DOWN").exists())
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        record = self.record()
        self.assertEqual((record["delivery_status"], record["delivered_seq"]), ("delivered", 2))
        self.assertEqual([e["state"] for e in self.bridge.events_for(self.session_id)], ["Waiting"])

    def test_any_later_successful_post_rearms_exhausted_sessions(self):
        other = self.sid("w1:pOther")
        seed(self.cache_mgr, {self.session_id: session(PANE, delivered=False, now=self.clock.time(),
                                                       delivery_status="retryable_exhausted", delivery_attempts=5),
                              other: session("w1:pOther", delivered=False, now=self.clock.time())})
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        record = self.record()
        self.assertEqual((record["delivery_status"], record["delivery_attempts"]), ("in_flight", 0))
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertEqual(self.record()["delivery_status"], "delivered")


class LeaseDisciplineTests(RetryCase):
    def test_sessions_are_claimed_one_at_a_time(self):
        """Gap reconciler-lease-batch-claim: while the first session is in flight the second is not leased yet."""
        second = self.sid("w1:pSecond")
        seed(self.cache_mgr, {self.session_id: session(PANE, delivered=False, now=self.clock.time()),
                              second: session("w1:pSecond", delivered=False, now=self.clock.time())})
        seen = []

        def on_post(payload):
            sessions = read_cache(self.cache_mgr)["sessions"]
            seen.append({sid: bool(rec.get("lease_token")) for sid, rec in sessions.items()})

        self.bridge.on_post = on_post
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertEqual(len(seen), 2)
        self.assertEqual(sum(seen[0].values()), 1, "exactly the session being sent holds a lease")

    def test_live_holder_keeps_the_lease_until_deadline_plus_grace(self):
        """Plan §1 rows 4-6: a live foreign holder defers the reconciler until deadline + 0.5s, then is taken over."""
        holder = self.add_fake_process("other-sender", live=True)
        seed(self.cache_mgr, {self.session_id: session(
            PANE, delivered=False, now=self.clock.time(), sending_pid=holder,
            lease_token=f"{holder}:None:1.0:{self.session_id}", lease_deadline=self.clock.time() - 0.3)})
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertEqual(self.bridge.events_for(self.session_id), [], "row 5: inside the 0.5s grace")
        self.clock.advance(0.3)
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertEqual(len(self.bridge.events_for(self.session_id)), 1, "row 6: hung holder taken over")
        self.assertEqual(self.record()["delivery_status"], "delivered")


class BackgroundBudgetTests(RetryCase):
    def test_reconciler_never_inherits_the_event_deadline(self):
        """Gaps bg-deadline-clamp / background-budget-starvation: started like an event process (1.5s deadline, already
        long past), the reconciler still locks with its own 5s budget and sends with the 0.2s socket timeout."""
        runtime.PROCESS_DEADLINE_SECONDS = runtime.DEFAULT_DEADLINE_SECONDS
        runtime.START_TIME = clock.monotonic() - 10.0
        seed(self.cache_mgr, {self.session_id: session(PANE, delivered=False, now=self.clock.time())})
        timeouts, lock_budgets = [], []
        real_send = step_b.send_event
        real_timeout = cache.BoundedSessionCache.effective_lock_timeout

        def send(payload, timeout, bridge_url=None):
            timeouts.append(timeout)
            return real_send(payload, timeout=timeout, bridge_url=bridge_url)

        def lock_budget(cache_self):
            lock_budgets.append(real_timeout(cache_self))
            return lock_budgets[-1]

        with mock.patch.object(step_b, "send_event", side_effect=send), \
                mock.patch.object(cache.BoundedSessionCache, "effective_lock_timeout", lock_budget):
            run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        self.assertEqual(timeouts, [0.2])
        self.assertEqual(set(lock_budgets), {cache.UNBOUNDED_LOCK_TIMEOUT})
        self.assertEqual(self.record()["delivery_status"], "delivered")
        self.assertIsNone(runtime.DEADLINE_MODE, "the unbounded mode is scoped to the run")

    def test_cache_lock_never_proceeds_unlocked_in_background(self):
        """A lock that stays contended past the background budget raises (the loop backs off); nothing is sent."""
        seed(self.cache_mgr, {self.session_id: session(PANE, delivered=False, now=self.clock.time())})
        hold_lock(self, self.cache_mgr.lock_file)
        with mock.patch.object(background, "_retry_after_cache_error", return_value=False):
            run_reconcile_background(bridge_url=self.mock_url)
        self.assertEqual(self.bridge.events_for(self.session_id), [])


class BackgroundLockWaitTests(SandboxTestCase):
    """Real clock: a child process really holds the cache lock for 0.3s."""

    def test_reconciler_waits_out_a_briefly_held_cache_lock(self):
        """Started inside the event window (deadline_bounded() would be inferred True), the reconciler still waits
        with its own bounded 5s lock budget for a brief holder instead of failing the pass after 0.2s."""
        sid = self.sid(PANE)
        seed(self.cache_mgr, {sid: session(PANE, delivered=False, now=clock.time())})
        runtime.PROCESS_DEADLINE_SECONDS = runtime.DEFAULT_DEADLINE_SECONDS
        runtime.START_TIME = clock.monotonic()
        hold_lock(self, self.cache_mgr.lock_file, seconds=0.3)
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        self.assertEqual(read_cache(self.cache_mgr)["sessions"][sid]["delivery_status"], "delivered")


if __name__ == "__main__":
    unittest.main()
