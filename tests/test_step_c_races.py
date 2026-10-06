"""Step C races driven through the REAL sender code (Plan §10.1 #6, #42, #43, #48, #63; §4.3 Step C).

A concurrent sender acts while a real POST is in flight, through the mock bridge's ``on_post`` /
``after_apply`` hooks (server thread, client blocked on its request), or by running the real Step C
(``sender.settle``) of a second claim. Assertions cover the cache and the bridge's ordered request log.
Gaps: t42-43-48-63-hollow, t6-drain-supersession, post-lock-dispatch-skipped-by-break,
pending-comp-target-generation.
"""

import json
import time
import unittest
from unittest import mock

from herdr_bartender import clock, runtime
from herdr_bartender.bridge import DeliveryResult
from herdr_bartender.cache import LockTimeout
from herdr_bartender.handlers import handle_agent_status_changed, handle_pane_closed
from herdr_bartender.orphans import export_orphan_record
from herdr_bartender.paths import get_orphan_path
from herdr_bartender.sanitize import get_hex_pane_id
from herdr_bartender.sender import (
    BACKGROUND_POLICY,
    EVENT_POLICY,
    Claim,
    SendPolicy,
    Sent,
    Staged,
    Target,
    compensate,
    dispatch,
    run_step_a,
    settle,
    transmit,
)
from herdr_bartender.sender import lease as lease_module
from tests.support import SandboxTestCase
from tests.support.probes import cache_lock_probe

PANE = "w1:pRace"
SLOWER_THAN_THE_SOCKET_CAP = 0.25   # > policy.SOCKET_CAP_SECONDS (0.2s)
HOOK_WAIT_SECONDS = 30.0            # socket timeout / lease length that outlast a cross-process hook


def _status(status="working", pane=PANE, **extra):
    return {"agent_status": status, "pane_id": pane, "workspace_id": "w1", "agent": "claude", **extra}


def _once(predicate, action):
    """A bridge hook that runs ``action`` the first time ``predicate(payload)`` holds."""
    fired = []

    def hook(payload):
        if not fired and isinstance(payload, dict) and predicate(payload):
            fired.append(payload)
            action(payload)
    return hook


class RaceCase(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.session_id = self.sid(PANE)
        self.bridge.probe = cache_lock_probe(self.cache_mgr.lock_file)

    def _session(self):
        with self.cache_mgr as data:
            return data["sessions"].get(self.session_id)

    def _cache(self):
        with self.cache_mgr as data:
            return data

    def _requests(self):
        return [r for r in self.bridge.requests if (r["body"] or {}).get("session_id") == self.session_id]

    def _arrived(self):
        """POST bodies for the session in the order they reached the bridge."""
        return [b for b in self.bridge.arrivals if isinstance(b, dict) and b.get("session_id") == self.session_id]

    @property
    def pending(self):
        return self.state_dir / "reconciler.pending"

    def _delivered_readmission(self) -> dict:
        """A live re-admission of the session, recorded as delivered (Bartender may still show none for it)."""
        return {"pane_id": PANE, "desired_state": "Working", "seq": 1, "delivered_seq": 1,
                "delivered_state": "Working", "delivery_status": "delivered", "generation": 2, "admitted_at_ns": 2,
                "last_event_at": time.time(),
                "desired_payload": {"state": "Working", "agent": "Claude (Herdr)", "session_id": self.session_id,
                                    "seq": 1}}

    def _seed_landed_attempt_and_readmission(self) -> dict:
        """An owed compensation whose earlier attempt may have landed (``posted``) plus a delivered re-admission."""
        entry = {"session_id": self.session_id, "pane_id": PANE, "agent": "Claude (Herdr)", "generation": 1,
                 "admitted_at_ns": 1, "timestamp": time.time()}
        with self.cache_mgr as data:
            data["pending_compensations"] = [{**entry, "attempts": 1, "last_attempt": time.time(), "posted": True}]
            data["sessions"][self.session_id] = self._delivered_readmission()
            self.cache_mgr.save(data)
        return entry

    def _assert_forced_resync(self) -> None:
        s = self._session()
        self.assertEqual((s["resync_generation"], s["delivered_seq"], s["delivery_status"]), (1, 0, "in_flight"))
        self.assertEqual(self._cache()["pending_compensations"], [])


class LeaseSupersessionTests(RaceCase):
    def test_p42_lease_taken_over_during_send_forces_resync(self):
        """Plan §10.1 #42: while our Working is in flight another sender takes the lease; Step C bumps
        resync_generation, forces delivered_seq=0 / in_flight, flags and ensures the reconciler."""
        def take_over(_payload):
            with self.cache_mgr as data:
                data["sessions"][self.session_id].update({"lease_token": "4242:None:1.0:other", "sending_pid": 4242})
                self.cache_mgr.save(data)

        self.bridge.on_post = _once(lambda p: p.get("state") == "Working", take_over)
        handle_agent_status_changed(_status(), {}, bridge_url=self.mock_url)
        s = self._session()
        self.assertEqual((s["resync_generation"], s["delivered_seq"], s["delivery_status"]), (1, 0, "in_flight"))
        self.assertEqual(s["lease_token"], "4242:None:1.0:other", "the new holder's lease is left alone")
        self.assertTrue(self.pending.exists())
        self.assertEqual(len(self.spawner.calls), 1, "post-lock hand-off ran (gap post-lock-dispatch-skipped-by-break)")
        self.assertEqual([r["body"]["state"] for r in self._requests()], ["Working"])

    def test_p63_hung_sender_step_c_makes_the_superseding_success_resync(self):
        """Plan §10.1 #63: B took over a hung sender A's lease (row 6). A's real Step C lands while B's POST is in
        flight and bumps resync_generation; B's success then cannot claim delivery and forces a re-sync."""
        holder = self.add_fake_process("hung-sender", live=True)
        token_a = f"{holder}:None:1.0:{self.session_id}"
        with self.cache_mgr as data:
            data["sessions"][self.session_id] = {
                "pane_id": PANE, "workspace_id": "w1", "agent": "Claude (Herdr)", "raw_agent": "claude",
                "desired_state": "Working", "seq": 1, "delivered_seq": 0, "generation": 1, "admitted_at_ns": 1,
                "desired_payload": {"state": "Working", "agent": "Claude (Herdr)", "session_id": self.session_id,
                                    "seq": 1},
                "lease_token": token_a, "sending_pid": holder, "lease_deadline": time.time() - 5.0,
                "delivery_status": "in_flight", "last_arrival_ns": 1, "last_event_ns": 1, "last_event_at": time.time(),
            }
            data["pane_generations"][PANE] = 1
            self.cache_mgr.save(data)
        claim_a = Claim(self.session_id, PANE, {"state": "Working", "seq": 1}, "Working", 1, token_a, 0, 1, 1, 1)

        def hung_sender_lands(_payload):
            settle(EVENT_POLICY.cache(self.state_dir), claim_a, Sent(DeliveryResult("success", None, 200), 1),
                   EVENT_POLICY)

        self.bridge.on_post = _once(lambda p: p.get("state") == "Waiting", hung_sender_lands)
        handle_agent_status_changed(_status("blocked"), {}, bridge_url=self.mock_url)
        s = self._session()
        self.assertEqual((s["resync_generation"], s["delivered_seq"], s["delivery_status"]), (1, 0, "in_flight"))
        self.assertEqual((s["seq"], s["desired_state"]), (2, "Waiting"))
        self.assertIsNone(s["lease_token"], "B releases its lease after the forced re-sync")
        self.assertTrue(self.pending.exists())

    def test_hung_sender_landing_after_the_takeover_delivery_forces_resync(self):
        """Plan §1 row 6 ("hung attempt will abort via token verification in Step C") + §4.3 Step C: B takes over hung
        sender A's lease and delivers seq 2; A's seq-1 Working then reaches Bartender LAST. A's Step C must force the
        re-sync (not drop it as stale below delivered_seq), so the reconciler restores B's state on Bartender."""
        from herdr_bartender.reconciler import reconcile_active_sessions
        handle_agent_status_changed(_status(), {}, bridge_url=self.mock_url)
        holder = self.add_fake_process("hung-sender", live=True)
        token_a = f"{holder}:None:1.0:{self.session_id}"
        with self.cache_mgr as data:  # A (re)claimed seq 1 (Working) and hung past deadline + grace
            data["sessions"][self.session_id].update({"lease_token": token_a, "sending_pid": holder,
                                                      "lease_deadline": time.time() - 5.0})
            self.cache_mgr.save(data)
        claim_a = Claim(self.session_id, PANE, {"state": "Working", "agent": "Claude (Herdr)",
                                                "session_id": self.session_id, "seq": 1},
                        "Working", 1, token_a, 0, 1, 1, 1)
        handle_agent_status_changed(_status("blocked"), {}, bridge_url=self.mock_url)  # B: takeover, seq 2
        self.assertEqual((self._session()["delivered_seq"], self.bridge.sessions[self.session_id]["state"]),
                         (2, "Waiting"))
        sent_a = transmit(claim_a, EVENT_POLICY, self.mock_url)  # A's POST lands last
        self.assertEqual(self.bridge.sessions[self.session_id]["state"], "Working")
        post = settle(EVENT_POLICY.cache(self.state_dir), claim_a, sent_a, EVENT_POLICY)
        s = self._session()
        self.assertEqual((s.get("resync_generation"), s["delivered_seq"], s["delivery_status"]), (1, 0, "in_flight"))
        self.assertTrue(post.hand_off, "the forced re-sync is handed to the reconciler")
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertEqual(self.bridge.sessions[self.session_id]["state"], "Waiting", "B's state restored on Bartender")
        self.assertEqual(self._session()["delivered_seq"], 2)


class CompensationTests(RaceCase):
    """Same-PID races: the concurrent close runs in this process (mock bridge thread), so it treats our lease as its
    own and claims the session (evicted branch). A close from another process defers to our live lease instead and
    reaches Step C through the tombstoned branch: tests/test_status_close_race.py."""

    def _close_during_working(self):
        """While our Working (seq 1) is in flight, a real pane.closed confirms its Ended and evicts the session."""
        closes = _once(lambda p: p.get("state") == "Working",
                       lambda _p: handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url))
        self.bridge.on_post = closes

    def test_p43_send_landing_after_eviction_is_compensated_outside_the_lock(self):
        """Plan §10.1 #43: Step C finds the session evicted, persists the compensating Ended, and the post-lock
        dispatch sends it (outside the lock) and only then clears it."""
        self._close_during_working()
        handle_agent_status_changed(_status(), {}, bridge_url=self.mock_url)
        arrived = self._arrived()
        self.assertEqual([b["state"] for b in arrived], ["Working", "Ended", "Ended"])
        self.assertEqual(sorted(arrived[2]), ["agent", "session_id", "state"], "compensation uses the minimal payload")
        compensation = self._requests()[2]
        self.assertEqual((compensation["in_critical_section"], compensation["cache_lock_free"]), (False, True))
        data = self._cache()
        self.assertEqual(data["pending_compensations"], [])
        self.assertNotIn(self.session_id, data["sessions"])
        self.assertNotIn(self.session_id, self.bridge.sessions, "the phantom Working was dismissed")

    def test_compensation_entry_targets_the_evicted_generation(self):
        """Gap pending-comp-target-generation: the persisted entry carries the real generation and admitted_at_ns."""
        with self.cache_mgr as data:
            data["next_generation"] = 5
            self.cache_mgr.save(data)
        self._close_during_working()
        with mock.patch.object(dispatch, "send_event", return_value=DeliveryResult("retryable", "5xx_server_error", 500)):
            handle_agent_status_changed(_status(), {}, bridge_url=self.mock_url, arrival_ns=123456789)
        (entry,) = self._cache()["pending_compensations"]
        self.assertEqual((entry["session_id"], entry["generation"], entry["admitted_at_ns"]),
                         (self.session_id, 6, 123456789))
        self.assertTrue(self.spawner.calls, "an owed compensation is handed to the reconciler")

    def test_p48_readmission_before_reverify_aborts_the_compensation(self):
        """Plan §10.1 #48: the pane is re-admitted between Step C and the re-verification: the compensation is
        aborted under the lock (entry cleared, nothing sent) and the live session survives on Bartender."""
        self._close_during_working()
        real_reverify = dispatch._reverify

        def readmit_then_reverify(cache_mgr, entry):
            later = time.time_ns() + 2_000_000_000
            handle_agent_status_changed(_status(timestamp=later / 1e9), {}, bridge_url=self.mock_url,
                                        arrival_ns=later)
            return real_reverify(cache_mgr, entry)

        with mock.patch.object(dispatch, "_reverify", side_effect=readmit_then_reverify):
            handle_agent_status_changed(_status(), {}, bridge_url=self.mock_url)
        self.assertEqual([b["state"] for b in self._arrived()], ["Working", "Ended", "Working"])
        data = self._cache()
        self.assertEqual(data["pending_compensations"], [])
        self.assertEqual(data["sessions"][self.session_id]["desired_state"], "Working")
        self.assertIn(self.session_id, self.bridge.sessions, "the re-admitted session is not dismissed")

    def test_compensation_is_budget_gated(self):
        """Plan §4.3 / §6.1: the compensating Ended is a post-lock POST, so it needs time_remaining() > 0.3s; without
        budget the persisted entry stays for the reconciler, which is flagged and ensured."""
        def close_then_spend_the_budget(_payload):
            handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url)
            self.pending.unlink(missing_ok=True)
            self.spawner.reset()
            runtime.PROCESS_DEADLINE_SECONDS, runtime.START_TIME = 0.2, clock.monotonic()

        self.bridge.on_post = _once(lambda p: p.get("state") == "Working", close_then_spend_the_budget)
        handle_agent_status_changed(_status(), {}, bridge_url=self.mock_url)
        self.assertEqual([b["state"] for b in self._arrived()], ["Working", "Ended"], "no compensating POST")
        (entry,) = self._cache()["pending_compensations"]
        self.assertEqual(entry["session_id"], self.session_id)
        self.assertTrue(self.pending.exists())
        self.assertEqual(len(self.spawner.calls), 1)

    def test_readmission_sent_while_the_compensation_is_in_flight_is_resynced(self):
        """Plan L298 / L564 post-compensation re-sync: a re-admission claimed and POSTed while the compensating Ended
        is in flight (Bartender applies the Ended last) must not be recorded as delivered: the re-sync bumps
        resync_generation, so the new sender's Step C success re-syncs instead of claiming delivery."""
        entry = {"session_id": self.session_id, "pane_id": PANE, "agent": "Claude (Herdr)", "generation": 1,
                 "admitted_at_ns": 1, "timestamp": time.time()}
        with self.cache_mgr as data:
            data["pending_compensations"] = [entry]
            self.cache_mgr.save(data)
        cache_mgr, readmitted = EVENT_POLICY.cache(self.state_dir), {}

        def readmit(data):
            data["sessions"][self.session_id] = {
                "pane_id": PANE, "desired_state": "Working", "seq": 1, "delivered_seq": 0, "generation": 2,
                "admitted_at_ns": 2, "delivery_status": "in_flight", "last_event_at": time.time(),
                "desired_payload": {"state": "Working", "agent": "Claude (Herdr)", "session_id": self.session_id,
                                    "seq": 1}}
            return Staged((Target(self.session_id, PANE, data["sessions"][self.session_id]),), mutated=True)

        def readmit_and_send(_payload):  # another sender's Step A and Step B; its Working lands before the Ended
            (claim,) = run_step_a(cache_mgr, readmit, policy=EVENT_POLICY, arrival_ns=2).claims
            readmitted.update(claim=claim, sent=transmit(claim, EVENT_POLICY, self.mock_url))

        self.bridge.on_post = _once(lambda p: p.get("state") == "Ended", readmit_and_send)
        self.assertTrue(compensate(cache_mgr, entry, EVENT_POLICY, self.mock_url), "the race is handed off")
        post = settle(cache_mgr, readmitted["claim"], readmitted["sent"], EVENT_POLICY)
        self.assertNotIn(self.session_id, self.bridge.sessions, "Bartender applied the compensating Ended last")
        s = self._session()
        self.assertEqual((s["delivered_seq"], s["delivery_status"], s["resync_generation"]), (0, "in_flight", 1))
        self.assertTrue(post.hand_off, "the re-synced Working is handed to the reconciler")
        self.assertEqual(self._cache()["pending_compensations"], [])

    def _seed_compensation(self):
        entry = {"session_id": self.session_id, "pane_id": PANE, "agent": "Claude (Herdr)", "generation": 1,
                 "admitted_at_ns": 1, "timestamp": time.time()}
        with self.cache_mgr as data:
            data["pending_compensations"] = [entry]
            self.cache_mgr.save(data)
        return entry

    def _readmit_while_the_ended_is_in_flight(self):
        """A re-admission is claimed, sent and settled as delivered while the compensating Ended is in flight; its
        Working reaches Bartender first, so Bartender applies the Ended last and shows no session."""
        def readmit(_payload):
            later = time.time_ns() + 2_000_000_000
            handle_agent_status_changed(_status(timestamp=later / 1e9), {}, bridge_url=self.mock_url,
                                        arrival_ns=later)
        self.bridge.on_post = _once(lambda p: p.get("state") == "Ended", readmit)

    def _assert_drain_restores_the_readmission(self):
        from herdr_bartender.reconciler import reconcile_active_sessions
        self.bridge.on_post = None
        self.assertNotIn(self.session_id, self.bridge.sessions, "Bartender applied the compensating Ended last")
        self.assertEqual(self._session()["delivery_status"], "delivered", "the re-admission was recorded as delivered")
        self.pending.unlink(missing_ok=True)
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertEqual(self._cache()["pending_compensations"], [])
        self.assertEqual(self.bridge.sessions.get(self.session_id, {}).get("state"), "Working",
                         "the drain must re-sync the live session the landed Ended dismissed")
        self.assertTrue(self.pending.exists(), "the forced re-sync is handed off (not left to the same pass's sweep)")

    def test_landed_attempt_superseded_by_a_readmission_is_resynced_and_handed_off(self):
        """Finding (RESYNCED hand-off): the re-verification finds a live re-admission after an attempt that may have
        landed. It forces the re-sync (no POST) AND reports the hand-off on both policies, so the re-send never waits
        for an unrelated pass."""
        for policy in (EVENT_POLICY, BACKGROUND_POLICY):
            with self.subTest(policy=policy.name):
                entry = self._seed_landed_attempt_and_readmission()
                self.assertTrue(compensate(policy.cache(self.state_dir), entry, policy, self.mock_url),
                                "the forced re-sync must be handed off")
                self._assert_forced_resync()
                self.assertEqual(self._arrived(), [], "the re-verification sends nothing")

    def test_landed_compensation_whose_settle_failed_resyncs_a_later_readmission(self):
        """Finding (unsettled landed compensation): the Ended landed but its settle could not lock, so the entry stays
        owed; a re-admission delivered meanwhile must be re-synced when the reconciler drains the entry, not merely
        spared (the attempt is persisted under the re-verification lock BEFORE the POST)."""
        entry = self._seed_compensation()
        self._readmit_while_the_ended_is_in_flight()
        with mock.patch.object(dispatch, "_settle", side_effect=LockTimeout("contended")):
            self.assertTrue(compensate(EVENT_POLICY.cache(self.state_dir), entry, EVENT_POLICY, self.mock_url))
        (owed,) = self._cache()["pending_compensations"]
        self.assertTrue(owed.get("posted"), "the attempt was recorded before the POST")
        self._assert_drain_restores_the_readmission()

    def test_retryable_compensation_that_landed_resyncs_a_later_readmission(self):
        """Finding (retryable compensation outcome): a socket timeout or 5xx may still have landed the Ended; the
        re-admission delivered meanwhile must be re-synced when the entry is drained."""
        entry = self._seed_compensation()
        self._readmit_while_the_ended_is_in_flight()
        real_send = dispatch.send_event

        def landed_then_timed_out(payload, timeout=0.2, bridge_url=None):
            real_send(payload, timeout=timeout, bridge_url=bridge_url)
            return DeliveryResult("retryable", "network_timeout", None)

        with mock.patch.object(dispatch, "send_event", side_effect=landed_then_timed_out):
            self.assertTrue(compensate(EVENT_POLICY.cache(self.state_dir), entry, EVENT_POLICY, self.mock_url))
        self._assert_drain_restores_the_readmission()

    def test_reverify_aborts_for_a_newer_generation(self):
        """Gap pending-comp-target-generation: a cached session of a newer generation (even one now closing)
        supersedes the compensation's target generation."""
        entry = {"session_id": self.session_id, "pane_id": PANE, "agent": "Claude (Herdr)", "generation": 6,
                 "admitted_at_ns": 1, "timestamp": time.time()}
        with self.cache_mgr as data:
            data["pending_compensations"] = [entry]
            data["sessions"][self.session_id] = {"pane_id": PANE, "desired_state": "Ended", "generation": 7, "seq": 2}
            self.cache_mgr.save(data)
        self.assertFalse(compensate(EVENT_POLICY.cache(self.state_dir), entry, EVENT_POLICY, self.mock_url))
        self.assertEqual(self._requests(), [])
        self.assertEqual(self._cache()["pending_compensations"], [])


class DrainSupersessionTests(RaceCase):
    def test_p06_new_turn_during_ended_send_is_not_evicted(self):
        """Plan §10.1 #6: while the pane.closed Ended is in flight (already applied by Bartender) a fresh working
        turn re-admits the pane and is delivered; the Ended's Step C must not evict the newer session.

        Same-PID race: the new turn runs in this process, so it claims our lease and delivers inline. The
        cross-process variant (deferral plus reconciler hand-off) is the test below.

        The new turn runs inside the bridge's ``after_apply`` hook while our Ended waits for its response, so our
        socket timeout must outlast it: under the real 0.2s cap a slow host (macOS fsync) times the Ended out and
        its minimal-payload retry would run while the hook's thread holds this process's cache lock."""
        handle_agent_status_changed(_status(), {}, bridge_url=self.mock_url)

        def new_turn(_payload):
            later = time.time_ns() + 2_000_000_000
            handle_agent_status_changed(_status(timestamp=later / 1e9), {}, bridge_url=self.mock_url,
                                        arrival_ns=later)

        self.bridge.after_apply = _once(lambda p: p.get("state") == "Ended", new_turn)
        with mock.patch.object(SendPolicy, "socket_timeout", lambda _policy: HOOK_WAIT_SECONDS):
            handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url)
        self.assertEqual([b["state"] for b in self._arrived()], ["Working", "Ended", "Working"])
        self.assertEqual(self.bridge.sessions[self.session_id]["state"], "Working", "final bridge state Working")
        s = self._session()
        self.assertIsNotNone(s, "the newer turn survives the late Ended confirmation")
        self.assertEqual((s["desired_state"], s["delivered_state"]), ("Working", "Working"))
        self.assertGreater(s["generation"], 1)

    def test_p06_new_turn_from_another_process_defers_and_is_handed_off(self):
        """Plan §10.1 #6, cross-process: the new turn's process defers to our live lease (no inline POST) and hands
        off; our Ended's Step C keeps the newer session, releases the lease and flags the undelivered Working.

        Deterministic by construction: the other process runs inside the bridge's ``after_apply`` hook while our
        Ended waits for its response, so our socket timeout must outlast it (the real 0.2s cap would time the Ended
        out first and let Step C run before, or concurrently with, the other process) and our lease must stay live
        for its whole run. The hook is slowed past the real cap so the race window always opens, and its outcome is
        asserted here on the main thread (an assertion in the bridge thread cannot fail the test)."""
        handle_agent_status_changed(_status(), {}, bridge_url=self.mock_url)
        self.spawner.reset()
        elsewhere = {}

        def new_turn_elsewhere(_payload):
            time.sleep(SLOWER_THAN_THE_SOCKET_CAP)
            envelope = {"event": "pane.agent_status_changed", "data": _status(), "context": {}}
            try:
                proc = self.run_cli("pane.agent_status_changed", input=json.dumps(envelope))
                elsewhere.update(returncode=proc.returncode, stderr=proc.stderr)
            except Exception as exc:  # noqa: BLE001 - surfaced on the main thread below
                elsewhere.update(error=repr(exc))

        self.bridge.after_apply = _once(lambda p: p.get("state") == "Ended", new_turn_elsewhere)
        with mock.patch.object(SendPolicy, "socket_timeout", lambda _policy: HOOK_WAIT_SECONDS), \
                mock.patch.object(lease_module, "LEASE_SECONDS", HOOK_WAIT_SECONDS):
            handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url)
        self.assertEqual(elsewhere.get("returncode"), 0, elsewhere)
        self.assertEqual([b["state"] for b in self._arrived()], ["Working", "Ended"], "the other process deferred")
        self.assertTrue(self.subprocess_spawns(), "the deferring process handed off to the reconciler")
        s = self._session()
        self.assertEqual((s["desired_state"], s["delivered_state"]), ("Working", "Ended"))
        self.assertLess(s["delivered_seq"], s["seq"])
        self.assertEqual((s["lease_token"], s["sending_pid"]), (None, None))
        self.assertTrue(self.pending.exists())
        self.assertEqual(len(self.spawner.calls), 1)


class WatchdogAfterStepCTests(RaceCase):
    def test_live_session_seen_only_by_step_c_ensures_the_watchdog(self):
        """Plan §4.3 L568: the watchdog check uses the state after Step C. Step A of the last pane's close sees no live
        session; a session admitted elsewhere while the Ended is in flight still gets the reconciler ensured."""
        handle_agent_status_changed(_status(), {}, bridge_url=self.mock_url)
        other = self.sid("w1:pOther")

        def admit_elsewhere(_payload):
            with self.cache_mgr as data:
                data["sessions"][other] = {"pane_id": "w1:pOther", "desired_state": "Working", "seq": 1,
                                           "delivered_seq": 1, "delivery_status": "delivered", "generation": 1,
                                           "last_event_at": time.time()}
                self.cache_mgr.save(data)

        self.bridge.after_apply = _once(lambda p: p.get("state") == "Ended", admit_elsewhere)
        self.spawner.reset()
        handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url)
        self.assertIsNone(self._session())
        self.assertFalse(self.pending.exists(), "nothing is owed")
        self.assertEqual(len(self.spawner.calls), 1, "the post-Step-C watchdog check ensured the reconciler")


class ConfirmedEndedDispatchTests(RaceCase):
    def test_confirmed_ended_removes_orphan_and_dismisses_vendor(self):
        """Gap post-lock-dispatch-skipped-by-break: after an Ended is confirmed and evicted, the orphan record is
        removed and the pane's vendor entry is dismissed (outside the lock)."""
        handle_agent_status_changed(_status(), {}, bridge_url=self.mock_url)
        export_orphan_record(self.session_id, {"pane_id": PANE, "desired_state": "Ended"})
        vendor = self.state_dir / "panes" / f"{get_hex_pane_id(PANE)}.vendor_active"
        vendor.write_text(json.dumps({"vendor_session_id": "vendor-uuid-0000000001"}))
        handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url)
        self.assertIsNone(self._session())
        self.assertFalse(get_orphan_path().exists() and self.session_id in get_orphan_path().read_text())
        dismissals = [r for r in self.bridge.requests if (r["body"] or {}).get("session_id") == "vendor-uuid-0000000001"]
        self.assertEqual([(r["body"], r["in_critical_section"]) for r in dismissals],
                         [({"state": "Ended", "agent": "Herdr", "session_id": "vendor-uuid-0000000001"}, False)])
        self.assertFalse(vendor.exists())
        self.assertEqual(self._cache()["dismissed_vendor_uuids"], {}, "confirmed dismissal purged (R11)")


class ReconcilerCompensationDrainTests(RaceCase):
    """Gap pending-compensation-cleared-before-send ("same fix in reconcile_active_sessions"): the reconciler drains
    persisted compensations through the sender's compensate(), so an entry is cleared only after its POST landed."""

    def _seed_entry(self):
        entry = {"session_id": self.session_id, "pane_id": PANE, "agent": "Claude (Herdr)", "generation": 3,
                 "admitted_at_ns": 1, "timestamp": time.time()}
        with self.cache_mgr as data:
            data["pending_compensations"] = [entry]
            self.cache_mgr.save(data)
        return entry

    def test_failed_drain_post_keeps_the_entry(self):
        """An unconfirmed POST keeps the entry (with its attempt recorded); it is retried once its 1s delay passed and
        cleared after the bridge confirmed it. A drain before the delay sends nothing (no busy retry)."""
        from herdr_bartender.reconciler import reconcile_active_sessions
        fake = self.use_fake_clock()
        self._seed_entry()
        self.bridge.return_code = 500
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        (owed,) = self._cache()["pending_compensations"]
        self.assertEqual((owed["attempts"], owed["posted"]), (1, True))
        self.bridge.return_code = 200
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertEqual(len(self._arrived()), 1, "not due yet: the retry waits for its delay")
        fake.advance(1.0)
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertEqual(self._cache()["pending_compensations"], [])
        self.assertEqual([b["state"] for b in self._arrived()], ["Ended", "Ended"])

    def test_drain_flags_a_resync_forced_by_the_reverification(self):
        """Finding (reconciler hand-off): draining an entry whose earlier attempt may have landed, while a re-admission
        is recorded as delivered, forces the re-sync and flags reconciler.pending. The drain runs without the sweep,
        so the flag can only come from compensate()'s hand-off."""
        from herdr_bartender.reconciler import drain_compensations
        self._seed_landed_attempt_and_readmission()
        self.assertTrue(drain_compensations(self.cache_mgr, bridge_url=self.mock_url))
        self._assert_forced_resync()
        self.assertTrue(self.pending.exists())
        self.assertEqual(self._arrived(), [], "nothing is sent by the drain itself")

    def test_drain_flags_a_readmission_raced_by_the_compensating_ended(self):
        """Finding (reconciler hand-off, settle race): a live session is admitted and recorded as delivered while the
        drained Ended is in flight (Bartender applies the Ended last). The settle forces the re-sync and the drain
        flags reconciler.pending."""
        from herdr_bartender.reconciler import drain_compensations
        self._seed_entry()

        def admit(_payload):
            with self.cache_mgr as data:
                data["sessions"][self.session_id] = {**self._delivered_readmission(), "generation": 4}
                self.cache_mgr.save(data)

        self.bridge.on_post = _once(lambda p: p.get("state") == "Ended", admit)
        self.assertTrue(drain_compensations(self.cache_mgr, bridge_url=self.mock_url))
        self.assertNotIn(self.session_id, self.bridge.sessions, "Bartender applied the compensating Ended last")
        self._assert_forced_resync()
        self.assertTrue(self.pending.exists())

    def test_drain_of_a_confirmed_compensation_does_not_flag(self):
        """A drained entry that landed with nothing re-admitted is simply cleared: no extra reconciler pass."""
        from herdr_bartender.reconciler import drain_compensations
        self._seed_entry()
        self.assertFalse(drain_compensations(self.cache_mgr, bridge_url=self.mock_url))
        self.assertEqual(self._cache()["pending_compensations"], [])
        self.assertFalse(self.pending.exists())


if __name__ == "__main__":
    unittest.main()
