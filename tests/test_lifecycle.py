"""Session lifecycle in the reconciler: TTLs, salvage horizon, Herdr dead/restart, Bartender re-sync, R12 horizon.

Gaps: t15-ttl, t21-bartender-resync, herdr-dead-5m-missing, bridge-cli/ttl-herdr-dead, herdr-restart-stale-payload,
start-time-fallback-false-restart, delivery-down-resync-missing, hard-horizon-unreachable, bridge-cli/orphan-horizon.
Every time horizon runs on the FakeClock (no real waits).
"""

import json
import unittest
from unittest import mock

from herdr_bartender import lifecycle, process, reconciler
from herdr_bartender.background import run_reconcile_background
from herdr_bartender.lifecycle import ORPHAN_HORIZON_SECONDS, TTL_SECONDS
from herdr_bartender.markers import touch_delivery_down, touch_pane_marker
from herdr_bartender.paths import get_orphan_path
from herdr_bartender.reconciler import reconcile_active_sessions
from herdr_bartender.snapshot import Instance, ProcessSnapshot
from tests.support import SandboxTestCase
from tests.support.reconciler_fixtures import pane_file, read_cache, salvaged, seed, session
from tests.support.sandbox import DEFAULT_BARTENDER_PID, DEFAULT_LSTART

DEAD_PID = 99_999_998


class LifecycleCase(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.clock = self.use_fake_clock()

    def reconcile(self):
        return reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)

    def advance(self, seconds):
        self.clock.advance(seconds)

    def states_sent(self, sid):
        return [e.get("state") for e in self.bridge.events_for(sid)]

    def sessions(self):
        return read_cache(self.cache_mgr)["sessions"]

    def kill_herdr(self):
        self.clear_fake_processes()
        self.add_fake_process("Bartender 6", pid=DEFAULT_BARTENDER_PID)
        process.reset_caches()


class TtlTests(LifecycleCase):
    def test_p15_ttl_exact_boundaries(self):
        """Plan §10.1 #15 (gap t15-ttl): retained at TTL-1s, staged Ended at TTL+1s - Working 12h, Waiting 48h,
        Idle/Done 24h - and the Ended (next seq, real Ended payload) is sent and the session evicted on HTTP 200."""
        for state, ttl in (("Working", 43200), ("Waiting", 172800), ("Idle", 86400), ("Done", 86400)):
            with self.subTest(state=state):
                pane = f"w1:p{state}"
                sid = self.sid(pane)
                seed(self.cache_mgr, {sid: session(pane, state, seq=3, now=self.clock.time())})
                self.assertEqual(TTL_SECONDS[state], ttl)
                self.advance(ttl - 1)
                self.reconcile()
                self.assertEqual(self.sessions()[sid]["desired_state"], state, "retained at TTL - 1s")
                self.assertEqual(self.states_sent(sid), [])
                self.advance(2)
                self.reconcile()
                self.assertNotIn(sid, self.sessions(), "evicted once its Ended was confirmed")
                (ended,) = self.bridge.events_for(sid)
                self.assertEqual((ended["state"], ended["seq"], ended["agent"]), ("Ended", 4, "Claude (Herdr)"))

    def test_salvaged_session_expires_after_300s_quiescent_horizon(self):
        """Plan §6.3 / §5.1 item 7: a salvaged record is never transmitted as Idle; after 300s it expires to Ended."""
        sid = self.sid("w1:pSalv")
        seed(self.cache_mgr, {sid: salvaged("w1:pSalv", now=self.clock.time())})
        self.advance(299)
        self.reconcile()
        self.assertEqual(self.states_sent(sid), [], "quiescent before the horizon")
        self.assertTrue(self.sessions()[sid]["salvaged"])
        self.advance(2)
        self.reconcile()
        self.assertEqual(self.states_sent(sid), ["Ended"])
        self.assertNotIn(sid, self.sessions())

    def test_salvaged_session_expires_at_once_when_herdr_is_dead(self):
        sid = self.sid("w1:pSalvDead")
        seed(self.cache_mgr, {sid: salvaged("w1:pSalvDead", now=self.clock.time())})
        self.kill_herdr()
        self.reconcile()
        self.assertEqual(self.states_sent(sid), ["Ended"])

    def test_ttl_is_strictly_greater_than(self):
        """Plan §3.3 TTL row: an age of exactly the TTL is retained (expiry needs age > TTL)."""
        self.clock.set_time(1_800_000_000.0)   # integral stamps: the age below is exactly 43200.0
        sid = self.sid("w1:pExact")
        seed(self.cache_mgr, {sid: session("w1:pExact", "Working", now=self.clock.time())})
        self.advance(43200)
        self.reconcile()
        self.assertEqual(self.sessions()[sid]["desired_state"], "Working")
        self.assertEqual(self.states_sent(sid), [])

    def test_backward_clock_step_never_extends_a_ttl(self):
        """A last_event_at in the future (stamped before a backward wall-clock step) starts aging now."""
        sid = self.sid("w1:pFuture")
        seed(self.cache_mgr, {sid: session("w1:pFuture", "Working", now=self.clock.time() + 3600)})
        self.reconcile()
        self.assertEqual(self.sessions()[sid]["last_event_at"], self.clock.time())
        self.advance(43201)
        self.reconcile()
        self.assertEqual(self.states_sent(sid), ["Ended"])

    def test_record_without_last_event_at_starts_aging(self):
        sid = self.sid("w1:pNoStamp")
        record = session("w1:pNoStamp", now=self.clock.time())
        del record["last_event_at"]
        seed(self.cache_mgr, {sid: record})
        self.reconcile()
        self.assertEqual(self.sessions()[sid]["last_event_at"], self.clock.time())


class HerdrDeadTests(LifecycleCase):
    def test_herdr_dead_more_than_5_minutes_expires_every_session(self):
        """Plan §5.1 item 7 (gaps herdr-dead-5m-missing, ttl-herdr-dead): herdr_dead_since is persisted; at 300s
        nothing expires, past 300s every live session is staged Ended (real Ended payload) and sent."""
        sids = {pane: self.sid(pane) for pane in ("w1:pD1", "w1:pD2")}
        seed(self.cache_mgr, {sids["w1:pD1"]: session("w1:pD1", "Working", now=self.clock.time()),
                              sids["w1:pD2"]: session("w1:pD2", "Waiting", seq=2, now=self.clock.time())})
        self.kill_herdr()
        self.reconcile()
        died = self.clock.time()
        self.assertEqual(read_cache(self.cache_mgr)["herdr_dead_since"], died)
        self.advance(300)
        process.reset_caches()
        self.reconcile()
        self.assertEqual({sid: self.states_sent(sid) for sid in sids.values()}, {sid: [] for sid in sids.values()})
        self.advance(1)
        process.reset_caches()
        self.reconcile()
        for sid in sids.values():
            self.assertEqual(self.states_sent(sid), ["Ended"], sid)
            self.assertNotIn(sid, self.sessions())

    def test_herdr_back_alive_clears_the_dead_timer(self):
        sid = self.sid("w1:pBack")
        seed(self.cache_mgr, {sid: session("w1:pBack", now=self.clock.time())})
        self.kill_herdr()
        self.reconcile()
        self.set_herdr_alive()
        self.advance(400)
        process.reset_caches()
        self.reconcile()
        self.assertIsNone(read_cache(self.cache_mgr)["herdr_dead_since"])
        self.assertEqual(self.states_sent(sid), [])

    def test_failed_probe_is_no_information(self):
        """R14 (gap start-time-fallback-false-restart): a failing pgrep neither starts the dead timer nor restarts."""
        sid = self.sid("w1:pProbe")
        seed(self.cache_mgr, {sid: session("w1:pProbe", now=self.clock.time())},
             last_herdr_pid=DEAD_PID, last_herdr_start_time="1")
        (self.sandbox / "pgrep.fail").touch()
        process.reset_caches()
        self.reconcile()
        data = read_cache(self.cache_mgr)
        self.assertIsNone(data["herdr_dead_since"])
        self.assertEqual(data["sessions"][sid]["desired_state"], "Working")


class HerdrRestartTests(LifecycleCase):
    def setUp(self):
        super().setUp()
        self.herdr_start = float(process.parse_lstart(DEFAULT_LSTART))   # the new instance's start (pgrep/ps shim)
        self.clock.set_time(self.herdr_start + 600)
        self.before = self.herdr_start - 60   # activity of the old instance

    def test_herdr_restart_sends_real_ended_payloads(self):
        """Plan §5.1 item 9 / R14 (gap herdr-restart-stale-payload): a new Herdr PID whose predecessor is confirmed dead
        expires every session of the old instance; the bridge receives state Ended (not the old Working/Waiting/Idle)
        and the sessions are evicted, salvaged ones included."""
        now = self.before
        sessions = {self.sid("w1:pR1"): session("w1:pR1", "Working", seq=4, now=now),
                    self.sid("w1:pR2"): session("w1:pR2", "Waiting", now=now),
                    self.sid("w1:pR3"): salvaged("w1:pR3", now=now)}
        seed(self.cache_mgr, sessions, last_herdr_pid=DEAD_PID, last_herdr_start_time="1")
        self.reconcile()
        for sid in sessions:
            self.assertEqual(self.states_sent(sid), ["Ended"], sid)
        self.assertEqual(self.sessions(), {})
        data = read_cache(self.cache_mgr)
        pid = process.get_herdr_pid()
        self.assertEqual(data["last_herdr_pid"], pid)
        self.assertEqual(data["herdr_instance_id"], f"{pid}:{process.get_process_start_time(pid)}")

    def test_same_herdr_instance_is_not_a_restart(self):
        sid = self.sid("w1:pSame")
        pid = process.get_herdr_pid()
        seed(self.cache_mgr, {sid: session("w1:pSame", now=self.clock.time())},
             last_herdr_pid=pid, last_herdr_start_time=process.get_process_start_time(pid))
        self.reconcile()
        self.assertEqual(self.states_sent(sid), [])

    def test_start_time_change_on_the_same_pid_is_a_restart(self):
        """R14: PID reuse defense - same PID, a different (known) start time."""
        sid = self.sid("w1:pReuse")
        pid = process.get_herdr_pid()
        seed(self.cache_mgr, {sid: session("w1:pReuse", now=self.before)},
             last_herdr_pid=pid, last_herdr_start_time="12345")
        self.reconcile()
        self.assertEqual(self.states_sent(sid), ["Ended"])

    def test_sessions_admitted_by_the_new_instance_survive_the_restart_detection(self):
        """Plan §5.1 item 9: the restart expires the OLD instance's sessions. A session the new instance admitted
        through the event path before the reconciler noticed the restart (last activity after the new start) is
        kept and not sent an Ended."""
        old, new = self.sid("w1:pOld"), self.sid("w1:pNew")
        seed(self.cache_mgr, {old: session("w1:pOld", now=self.before),
                              new: session("w1:pNew", now=self.herdr_start + 30)},
             last_herdr_pid=DEAD_PID, last_herdr_start_time="1")
        self.reconcile()
        self.assertEqual(self.states_sent(old), ["Ended"])
        self.assertEqual(self.states_sent(new), [])
        self.assertEqual(self.sessions()[new]["desired_state"], "Working")

    def test_unknown_new_start_time_expires_every_session(self):
        """No start time for the new instance: nothing tells the instances apart, so every live session expires."""
        sid = self.sid("w1:pUnknown")
        data = {"sessions": {sid: session("w1:pUnknown", now=self.herdr_start + 30)}}
        expired = lifecycle.expire_old_instance(data, self.clock.time(), None)
        self.assertEqual(expired, (sid,))


class BartenderResyncTests(LifecycleCase):
    def _seed_resync_case(self, last_pid, last_start):
        now = self.clock.time()
        self.live, self.quiet = self.sid("w1:pSyncTest"), self.sid("w1:pQuiet")
        seed(self.cache_mgr, {self.live: session("w1:pSyncTest", "Working", seq=2, now=now),
                              self.quiet: salvaged("w1:pQuiet", now=now)},
             last_bartender_pid=last_pid, last_bartender_start_time=last_start)
        self.bridge.history.clear()

    def test_p21_bartender_pid_change_resends_live_sessions(self):
        """Plan §10.1 #21 (gap t21-bartender-resync): the shim reports a new Bartender PID and the recorded one is dead:
        delivered_seq is reset and the live session's state is POSTed again; salvaged sessions are not; the new PID
        and start time are recorded."""
        self._seed_resync_case(99_999_999, "1")
        self.reconcile()
        self.assertEqual([(e["state"], e["seq"]) for e in self.bridge.events_for(self.live)], [("Working", 2)])
        self.assertEqual(self.bridge.events_for(self.quiet), [], "salvaged sessions stay quiescent")
        data = read_cache(self.cache_mgr)
        quiet = data["sessions"][self.quiet]
        self.assertEqual((quiet["delivered_seq"], quiet["delivery_status"], quiet.get("resync_generation", 0)),
                         (1, "salvaged", 0), "a salvaged record is not even re-armed for the re-sync")
        self.assertEqual((data["sessions"][self.live]["delivered_seq"], data["sessions"][self.live]["delivery_status"]),
                         (2, "delivered"))
        self.assertEqual(data["sessions"][self.live]["resync_generation"], 1)
        self.assertEqual(data["last_bartender_pid"], DEFAULT_BARTENDER_PID)
        self.assertEqual(data["last_bartender_start_time"], process.parse_lstart(DEFAULT_LSTART))

    def test_p21_old_bartender_still_alive_is_not_a_restart(self):
        """Negative case: the recorded Bartender instance is alive with the same start time - no re-sync."""
        old = self.add_fake_process("old-bartender", live=True)
        self._seed_resync_case(old, process.parse_lstart(DEFAULT_LSTART))
        self.reconcile()
        self.assertEqual(self.bridge.events_for(self.live), [])
        self.assertEqual(read_cache(self.cache_mgr)["last_bartender_pid"], old, "tracking keeps the live instance")

    def test_reconnection_after_delivery_down_is_a_full_resync(self):
        """Plan §1 L113 / §5.1 item 4 (gap delivery-down-resync-missing): /health ok while DELIVERY_DOWN was set
        clears it and re-sends every live, non-salvaged session."""
        self._seed_resync_case(DEFAULT_BARTENDER_PID, process.parse_lstart(DEFAULT_LSTART))
        touch_delivery_down()
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        self.assertFalse((self.state_dir / "DELIVERY_DOWN").exists())
        self.assertEqual([e["state"] for e in self.bridge.events_for(self.live)], ["Working"])
        self.assertEqual(self.bridge.events_for(self.quiet), [])


class OrphanHorizonTests(LifecycleCase):
    def _unconfirmed_ended(self, pane, **extra):
        fields = {"delivery_status": "retryable_exhausted", "delivery_attempts": 5, "orphaned_ended": True, **extra}
        return session(pane, "Ended", seq=3, delivered=False, now=self.clock.time(), **fields)

    def _orphans(self):
        path = get_orphan_path()
        return json.loads(path.read_text())["sessions"] if path.exists() else {}

    def test_ended_unconfirmed_12h_past_ttl_is_exported_and_evicted(self):
        """R12 (gaps hard-horizon-unreachable, orphan-horizon): retained 1s before the horizon, exported to the 0600
        orphan file and evicted (marker removed) 1s after it, whatever its delivery status."""
        self.assertEqual(ORPHAN_HORIZON_SECONDS, 43200)
        sid = self.sid("w1:pHz")
        seed(self.cache_mgr, {sid: self._unconfirmed_ended("w1:pHz", ttl_expired_at=self.clock.time())})
        touch_pane_marker("w1:pHz")
        self.advance(43199)
        self.reconcile()
        self.assertIn(sid, self.sessions())
        self.advance(2)
        self.reconcile()
        self.assertNotIn(sid, self.sessions())
        self.assertTrue(self._orphans()[sid]["orphaned_ended"])
        self.assertEqual(get_orphan_path().stat().st_mode & 0o777, 0o600)
        self.assertFalse(pane_file(self.state_dir, "w1:pHz").exists())

    def test_non_retryable_orphaned_ended_is_evicted_12h_after_orphaning(self):
        """R12 (gap orphan-horizon, non-retryable): a rejected close Ended (orphaned_at) leaves the cache after 12h."""
        sid = self.sid("w1:pRej")
        seed(self.cache_mgr, {sid: self._unconfirmed_ended("w1:pRej", orphaned_at=self.clock.time(),
                                                           delivery_status="non_retryable_failed")})
        self.advance(43201)
        self.reconcile()
        self.assertNotIn(sid, self.sessions())
        self.assertIn(sid, self._orphans())

    def test_undelivered_ttl_expiry_reaches_the_horizon_end_to_end(self):
        """A Working session whose TTL Ended keeps failing (500) is exported and evicted 12h after the expiry."""
        sid = self.sid("w1:pE2E")
        seed(self.cache_mgr, {sid: session("w1:pE2E", now=self.clock.time())})
        self.bridge.return_code = 500
        self.bridge.health_ok = False
        self.advance(43201)
        self.reconcile()
        expired = self.sessions()[sid]
        self.assertEqual((expired["desired_state"], expired["ttl_expired_at"]), ("Ended", self.clock.time()))
        self.advance(43201)
        self.reconcile()
        self.assertNotIn(sid, self.sessions())
        self.assertIn(sid, self._orphans())

    def test_horizon_counts_from_the_earlier_of_ttl_expiry_and_orphaning(self):
        sid = self.sid("w1:pTwo")
        t0 = self.clock.time()
        seed(self.cache_mgr, {sid: self._unconfirmed_ended("w1:pTwo", ttl_expired_at=t0, orphaned_at=t0 + 3600)})
        self.advance(43199)
        self.reconcile()
        self.assertIn(sid, self.sessions())
        self.advance(2)
        self.reconcile()
        self.assertNotIn(sid, self.sessions(), "12h after the earlier origin, not the later one")

    def test_an_export_that_was_not_written_is_never_evicted(self):
        """Export-before-evict: a session whose orphan write failed stays cached (and out of the file); the others
        are evicted."""
        lost, kept = self.sid("w1:pLost"), self.sid("w1:pKept")
        t0 = self.clock.time()
        seed(self.cache_mgr, {lost: self._unconfirmed_ended("w1:pLost", orphaned_at=t0),
                              kept: self._unconfirmed_ended("w1:pKept", orphaned_at=t0)})
        self.advance(43201)
        real = reconciler.export_orphan_record

        def export(sid, record, **kwargs):
            return False if sid == lost else real(sid, record, **kwargs)

        with mock.patch.object(reconciler, "export_orphan_record", side_effect=export):
            self.reconcile()
        self.assertIn(lost, self.sessions())
        self.assertNotIn(lost, self._orphans())
        self.assertNotIn(kept, self.sessions())
        self.assertIn(kept, self._orphans())

    def test_a_session_that_moved_on_during_the_export_is_not_evicted(self):
        """Evict-if-unchanged: a newer seq staged between the export and the eviction keeps the session cached."""
        sid = self.sid("w1:pMoved")
        seed(self.cache_mgr, {sid: self._unconfirmed_ended("w1:pMoved", orphaned_at=self.clock.time())})
        self.advance(43201)
        real = reconciler.export_orphan_record

        def export_then_move_on(*args, **kwargs):
            written = real(*args, **kwargs)
            with self.cache_mgr as data:
                data["sessions"][sid]["seq"] += 1
                self.cache_mgr.save(data)
            return written

        with mock.patch.object(reconciler, "export_orphan_record", side_effect=export_then_move_on):
            self.reconcile()
        self.assertIn(sid, self.sessions())

    def test_live_foreign_lease_defers_the_horizon_until_its_grace_ends(self):
        sid = self.sid("w1:pLeased")
        seed(self.cache_mgr, {sid: self._unconfirmed_ended("w1:pLeased", orphaned_at=self.clock.time())})
        self.advance(43201)
        with self.cache_mgr as data:
            data["sessions"][sid].update({"sending_pid": DEAD_PID, "lease_token": f"{DEAD_PID}:1:1.0:{sid}",
                                          "lease_deadline": self.clock.time() + 1.0})
            self.cache_mgr.save(data)
        self.reconcile()
        self.advance(1.4)
        self.reconcile()
        self.assertIn(sid, self.sessions(), "deadline + 0.5s grace not over yet")
        self.advance(0.2)
        self.reconcile()
        self.assertNotIn(sid, self.sessions())

    def test_readmitted_session_is_not_evicted(self):
        """The horizon only applies to an unconfirmed Ended: a live session is never evicted by it."""
        sid = self.sid("w1:pLive")
        seed(self.cache_mgr, {sid: session("w1:pLive", now=self.clock.time(), ttl_expired_at=1.0, orphaned_at=1.0)})
        self.reconcile()
        self.assertIn(sid, self.sessions())


class PureLifecycleTests(SandboxTestCase):
    """Pure functions, still sandboxed: expiry logs to the (sandboxed) state dir."""

    start_bridge = False

    def test_instance_restart_rules(self):
        """R14: PID change needs the old instance confirmed gone; same PID needs two known, different start times."""
        restarted = lifecycle.instance_restarted
        self.assertTrue(restarted(10, "5", Instance(11, "6", True), True))
        self.assertFalse(restarted(10, "5", Instance(11, "6", True), False))
        self.assertFalse(restarted(10, "5", Instance(11, "6", True), None))
        self.assertTrue(restarted(10, "5", Instance(10, "6", True), None))
        self.assertFalse(restarted(10, None, Instance(10, "6", True), None))
        self.assertFalse(restarted(10, "5", Instance(10, None, True), None))
        self.assertFalse(restarted(None, None, Instance(10, "6", True), None))
        self.assertFalse(restarted(10, "5", Instance(None, None, False), True))

    def test_expiry_builds_an_ended_payload_and_advances_seq(self):
        record = {"desired_state": "Working", "seq": 7, "agent": "Codex (Herdr)", "salvaged": True,
                  "desired_payload": {"state": "Working"}, "delivery_attempts": 3, "next_retry_at": 5.0}
        lifecycle.expire_session("sid", record, 100.0, lifecycle.REASON_TTL)
        self.assertEqual(record["desired_payload"], {"state": "Ended", "agent": "Codex (Herdr)", "session_id": "sid",
                                                     "seq": 8})
        self.assertEqual((record["seq"], record["salvaged"], record["delivery_status"], record["delivery_attempts"],
                          record["next_retry_at"], record["ttl_expired_at"]), (8, False, "in_flight", 0, None, 100.0))

    def test_herdr_dead_tracking_needs_a_confirmed_absence(self):
        data = {}
        dead = ProcessSnapshot(herdr=Instance(None, None, True), herdr_alive=False)
        unknown = ProcessSnapshot(herdr=Instance(None, None, False))
        self.assertFalse(lifecycle.track_herdr_dead(data, unknown, 10.0))
        self.assertNotIn("herdr_dead_since", data)
        self.assertFalse(lifecycle.track_herdr_dead(data, dead, 10.0))
        self.assertFalse(lifecycle.track_herdr_dead(data, dead, 310.0))
        self.assertTrue(lifecycle.track_herdr_dead(data, dead, 310.5))


if __name__ == "__main__":
    unittest.main()
