"""Background reconciler loop: singleton, wake-ups, idle exit, heartbeat, backoff, horizons, drains.

Gaps: tests-weak-reconciler, t11-helper-singleton, t54-drain-reverify, t57, premature-idle-exit, no-spawn-after-delivery,
reconciler-lease-batch-claim (see test_retry_schedule), terminal-horizon-no-evict. Every horizon runs on the FakeClock.
"""

import fcntl
import json
import os
import time
import unittest
from unittest import mock

from herdr_bartender import background, clock, handoff, log, process
from herdr_bartender.background import run_reconcile_background
from herdr_bartender.handlers import handle_agent_status_changed
from herdr_bartender.paths import get_orphan_path
from herdr_bartender.reconciler import reconcile_active_sessions
from herdr_bartender.schedule import CacheView
from herdr_bartender.snapshot import Instance, ProcessSnapshot
from tests.support import SandboxTestCase
from tests.support.reconciler_fixtures import LoopRunner, pane_file, read_cache, seed, session


class LoopCase(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.clock = self.use_fake_clock()
        self.pending = self.state_dir / "reconciler.pending"

    def sessions(self):
        return read_cache(self.cache_mgr)["sessions"]


class SingletonTests(LoopCase):
    def test_p11_second_runner_flags_pending_and_the_owner_completes_new_work(self):
        """Plan §10.1 #11 (gap t11-helper-singleton): while reconciler.lock is held a second runner only touches
        reconciler.pending. The owner then consumes it, delivers the newly arrived work and idles out on its own."""
        lock_fd = os.open(str(self.state_dir / "reconciler.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run_reconcile_background(bridge_url=self.mock_url)
        self.assertTrue(self.pending.exists())
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        sid = self.sid("w1:pNew")
        seed(self.cache_mgr, {sid: session("w1:pNew", "Ended", seq=2, delivered=False, now=self.clock.time())})
        runner = LoopRunner(self.state_dir)
        runner.run(bridge_url=self.mock_url)
        self.assertFalse(self.pending.exists())
        self.assertEqual([e["state"] for e in self.bridge.events_for(sid)], ["Ended"])
        self.assertEqual(self.sessions(), {})
        self.assertFalse((self.state_dir / "DISABLED").exists(), "the loop ended by its own idle exit")

    def test_spawned_reconciler_runs_the_loop_directly(self):
        """R21 + gap no-spawn-after-delivery: an event leaving a delivered live session ensures the reconciler, and
        the spawn (already detached) asks for the loop itself (--foreground), never a second detach."""
        handle_agent_status_changed({"agent_status": "working", "pane_id": "w1:pSpawn", "workspace_id": "w1",
                                     "agent": "claude"}, {}, bridge_url=self.mock_url)
        self.assertEqual(self.sessions()[self.sid("w1:pSpawn")]["delivery_status"], "delivered")
        self.assertEqual(self.spawner.calls, [handoff.loop_argv()])
        self.assertEqual(handoff.loop_argv()[-2:], ["--reconcile-background", "--foreground"])


class IdleExitTests(LoopCase):
    def test_idle_exit_only_after_60_seconds_with_a_healthy_bridge(self):
        """Plan §5.1 item 11 (gap premature-idle-exit): no sessions and a healthy bridge for 60 consecutive seconds."""
        runner = LoopRunner(self.state_dir)
        start = self.clock.time()
        runner.run(bridge_url=self.mock_url)
        self.assertGreaterEqual(self.clock.time() - start, 60.0)
        self.assertLess(self.clock.time() - start, 61.0)
        self.assertEqual(len(runner.sweep_times()), 4, "passes at 0, 20, 40, 60s")

    def test_unhealthy_bridge_or_pending_work_keeps_the_loop_alive(self):
        """Not idle: an unhealthy bridge (with or without DELIVERY_DOWN), an orphan record whose automatic replay is
        due while /health fails, a salvaged session awaiting its 300s horizon. (An orphan the bridge keeps rejecting
        backs off and no longer blocks the idle exit: tests/test_replay_backoff.py.)"""
        def unhealthy():
            self.bridge.health_ok = False
            (self.state_dir / "DELIVERY_DOWN").touch()

        def unhealthy_without_delivery_down():
            self.bridge.health_ok = False

        def orphan_due_while_unhealthy():
            self.bridge.health_ok = False
            get_orphan_path().write_text(json.dumps({"version": 1, "sessions": {self.sid("w1:pO"): {}}}))

        def salvaged_session():
            from tests.support.reconciler_fixtures import salvaged
            seed(self.cache_mgr, {self.sid("w1:pS"): salvaged("w1:pS", now=self.clock.time())})

        for name, arrange in (("unhealthy bridge", unhealthy), ("unhealthy, no DELIVERY_DOWN",
                                                                unhealthy_without_delivery_down),
                              ("orphan", orphan_due_while_unhealthy), ("salvaged", salvaged_session)):
            with self.subTest(name):
                arrange()
                start = self.clock.time()
                runner = LoopRunner(self.state_dir, stop=lambda: self.clock.time() - start > 300)
                runner.run(bridge_url=self.mock_url)
                self.assertGreater(self.clock.time() - start, 300, f"{name}: no idle exit")
                for leftover in (self.state_dir / "DISABLED", self.state_dir / "DELIVERY_DOWN", get_orphan_path()):
                    leftover.unlink(missing_ok=True)
                self.bridge.health_ok, self.bridge.return_code = True, 200

    def test_owed_side_effects_alone_keep_the_loop_from_idling(self):
        """Plan §5.1 item 11: no session, a healthy bridge, but a compensation/cleanup/dismissal still owed."""
        snap = ProcessSnapshot(bartender=Instance(7, "1", True), herdr=Instance(2, "1", True))
        owed = background.PassOutcome(snap, CacheView(sessions=0, owed=True), True)
        nothing = background.PassOutcome(snap, CacheView(sessions=0, owed=False), True)
        self.assertFalse(background._idle_now(self.state_dir, owed))
        self.assertTrue(background._idle_now(self.state_dir, nothing))

    def test_live_session_keeps_the_loop_alive(self):
        seed(self.cache_mgr, {self.sid("w1:pAlive"): session("w1:pAlive", now=self.clock.time())})
        start = self.clock.time()
        LoopRunner(self.state_dir, stop=lambda: self.clock.time() - start > 200).run(bridge_url=self.mock_url)
        self.assertGreater(self.clock.time() - start, 200)


class WakeLatencyTests(LoopCase):
    def test_pending_touch_wakes_the_sleeping_loop_within_half_a_second(self):
        """Plan §5.1 item 3: the loop sleeps in 0.5s ticks and a reconciler.pending touch breaks the sleep, so new work
        starts within the <=1.0s latency criterion; the touch resets the absence backoff."""
        seed(self.cache_mgr, {self.sid("w1:pLat"): session("w1:pLat", now=self.clock.time())})
        touched_at, real_sleep, ticks = [], clock.sleep, []

        def sleep(seconds):
            ticks.append(seconds)
            if len(ticks) == 7:
                self.pending.touch()
                touched_at.append(self.clock.time())
            real_sleep(seconds)

        runner = LoopRunner(self.state_dir, stop=lambda: bool(touched_at))
        with mock.patch.object(clock, "sleep", side_effect=sleep):
            runner.run(bridge_url=self.mock_url)
        first, woken = runner.sweep_times()[:2]
        self.assertLessEqual(woken - touched_at[0], 0.5)
        self.assertLess(woken - first, 20.0, "woken long before the 20s cadence")
        self.assertTrue(all(t <= 0.5 for t in ticks), "sleeps are 0.5s ticks")
        self.assertFalse(self.pending.exists(), "consumed by the woken pass")


    def test_spool_arrival_without_pending_forces_a_full_pass(self):
        """A spool envelope that arrives while the loop sleeps (no pending touch) wakes a full pass that replays it,
        never a burst of heartbeat-only passes."""
        from herdr_bartender import spool
        seed(self.cache_mgr, {self.sid("w1:pSpool"): session("w1:pSpool", now=self.clock.time())})
        real_sleep, ticks = clock.sleep, []

        def sleep(seconds):
            ticks.append(seconds)
            if len(ticks) == 5:
                spool.enqueue_spool("pane.closed", {"pane_id": "w1:pSpool"}, {}, arrival_ns=self.clock.time_ns())
                self.pending.unlink(missing_ok=True)
            real_sleep(seconds)

        runner = LoopRunner(self.state_dir, stop=lambda: self.sid("w1:pSpool") not in self.sessions())
        with mock.patch.object(clock, "sleep", side_effect=sleep):
            runner.run(bridge_url=self.mock_url)
        self.assertEqual([kind for _, kind in runner.passes], ["sweep", "sweep"])
        self.assertEqual([e["state"] for e in self.bridge.events_for(self.sid("w1:pSpool"))], ["Ended"])


class AbsenceTests(LoopCase):
    def _pass_outcome(self, snapshot, sessions=1):
        return background.PassOutcome(snapshot, CacheView(sessions=sessions), None)

    def test_r13_heartbeat_keeps_its_20s_cadence_during_the_300s_backoff(self):
        """Plan §5.1 item 11 + R13: after 1000s of Bartender absence full passes back off to 300s, while the marker
        heartbeat still runs every 20s; a reconciler.pending touch cancels the backoff."""
        absent = ProcessSnapshot(bartender=Instance(None, None, True), herdr=Instance(2, "1", True))
        calls = []

        def record(kind):
            def run(*_args):
                calls.append((self.clock.time(), kind))
                if self.clock.time() - calls[0][0] > 1700:
                    (self.state_dir / "DISABLED").touch()
                return self._pass_outcome(absent)
            return run

        with mock.patch.object(background, "_sweep_pass", side_effect=record("sweep")), \
                mock.patch.object(background, "_heartbeat_pass", side_effect=record("heartbeat")):
            run_reconcile_background(bridge_url=self.mock_url)
        start = calls[0][0]
        sweeps = [round(t - start) for t, kind in calls if kind == "sweep"]
        beats = [round(t - start) for t, kind in calls if kind == "heartbeat"]
        self.assertEqual(sweeps[:3], [0, 20, 40])
        late = [t for t in sweeps if t > 1100]
        self.assertTrue(late and all(b - a == 300 for a, b in zip(late, late[1:])), sweeps)
        gaps = sorted({b - a for a, b in zip(sorted(sweeps + beats), sorted(sweeps + beats)[1:]) if a > 1100})
        self.assertEqual(gaps, [20], "a heartbeat or a pass every 20s during the backoff")

    def test_bartender_return_ends_the_backoff_at_once(self):
        """Plan §5.1 item 11: once Bartender runs again the absence counter resets and a full pass follows within a
        tick, instead of waiting out the rest of a 300s backoff sleep."""
        absent = ProcessSnapshot(bartender=Instance(None, None, True), herdr=Instance(2, "1", True))
        present = ProcessSnapshot(bartender=Instance(7, "1", True), herdr=Instance(2, "1", True))
        calls, back_at = [], []

        def record(kind):
            def run(*_args):
                calls.append((self.clock.time(), kind))
                elapsed = self.clock.time() - calls[0][0]
                if elapsed > 1500 and not back_at:
                    back_at.append(self.clock.time())
                if back_at and self.clock.time() - back_at[0] > 30:
                    (self.state_dir / "DISABLED").touch()
                return self._pass_outcome(present if back_at else absent)
            return run

        with mock.patch.object(background, "_sweep_pass", side_effect=record("sweep")), \
                mock.patch.object(background, "_heartbeat_pass", side_effect=record("heartbeat")):
            run_reconcile_background(bridge_url=self.mock_url)
        after = [(round(t - back_at[0], 1), kind) for t, kind in calls if t > back_at[0]]
        self.assertEqual(after[0], (0.5, "sweep"), after)

    def test_terminal_horizon_exports_evicts_and_exits(self):
        """Plan §5.1 item 11 (gap terminal-horizon-no-evict): Bartender absent >12h (43200s, strictly) and Herdr dead -
        every session is exported to the 0600 orphan file, evicted with its marker, and the loop exits."""
        sid = self.sid("w1:pTerm")
        seed(self.cache_mgr, {sid: session("w1:pTerm", "Ended", seq=2, delivered=False, now=self.clock.time())})
        pane_file(self.state_dir, "w1:pTerm").parent.mkdir(parents=True, exist_ok=True)
        pane_file(self.state_dir, "w1:pTerm").write_text("1")
        gone = ProcessSnapshot(bartender=Instance(None, None, True), herdr=Instance(None, None, True),
                               herdr_alive=False)
        start, passes, cached_at_43199 = self.clock.time(), [], []

        def run(*_args):
            passes.append(self.clock.time())
            if len(passes) == 2:
                self.clock.set_time(start + 43199)
            elif len(passes) == 3:
                cached_at_43199.append(sid in self.sessions())
                self.clock.set_time(start + 43201)
            elif len(passes) > 3:
                (self.state_dir / "DISABLED").touch()
            return self._pass_outcome(gone)

        with mock.patch.object(background, "_sweep_pass", side_effect=run), \
                mock.patch.object(background, "_heartbeat_pass", side_effect=run):
            run_reconcile_background(bridge_url=self.mock_url)
        self.assertEqual(cached_at_43199, [True], "no exit at 43199s of absence")
        self.assertEqual(len(passes), 3, "exited right after 43201s")
        self.assertFalse((self.state_dir / "DISABLED").exists())
        self.assertEqual(self.sessions(), {})
        exported = json.loads(get_orphan_path().read_text())["sessions"]
        self.assertEqual((exported[sid]["desired_state"], exported[sid]["orphaned_ended"]), ("Ended", True))
        self.assertEqual(get_orphan_path().stat().st_mode & 0o777, 0o600)
        self.assertFalse(pane_file(self.state_dir, "w1:pTerm").exists())

    def test_no_terminal_exit_while_herdr_is_alive(self):
        sid = self.sid("w1:pAlive12h")
        seed(self.cache_mgr, {sid: session("w1:pAlive12h", now=self.clock.time())})
        absent = ProcessSnapshot(bartender=Instance(None, None, True), herdr=Instance(2, "1", True))
        passes = []

        def run(*_args):
            passes.append(self.clock.time())
            self.clock.advance(5000)
            if len(passes) > 12:
                (self.state_dir / "DISABLED").touch()
            return self._pass_outcome(absent)

        with mock.patch.object(background, "_sweep_pass", side_effect=run), \
                mock.patch.object(background, "_heartbeat_pass", side_effect=run):
            run_reconcile_background(bridge_url=self.mock_url)
        self.assertGreater(passes[-1] - passes[0], 43200)
        self.assertEqual(len(passes), 13, "the loop ran until DISABLED, not into a terminal exit")
        self.assertIn(sid, self.sessions())
        self.assertFalse(get_orphan_path().exists())


class HeartbeatTests(LoopCase):
    def test_p57_heartbeat_refreshes_working_waiting_idle_and_done_markers(self):
        """Plan §10.1 #57 (gap t1-19-57-62-minor): every delivered live state - Idle and Done included - has its
        90s-old marker refreshed while Herdr is alive; salvaged (even one whose status reads "delivered"), Ended and
        undelivered sessions do not."""
        now = self.clock.time()
        states = {"Working": True, "Waiting": True, "Idle": True, "Done": True}
        records = {self.sid(f"w1:p{s}"): session(f"w1:p{s}", s, now=now) for s in states}
        records[self.sid("w1:pUndelivered")] = session("w1:pUndelivered", delivered=False, now=now,
                                                       delivery_attempts=4, next_retry_at=now + 8)
        records[self.sid("w1:pSalvagedDelivered")] = session("w1:pSalvagedDelivered", "Idle", now=now, salvaged=True)
        records[self.sid("w1:pEndedDelivered")] = session("w1:pEndedDelivered", "Ended", seq=2, now=now,
                                                          delivery_status="non_retryable_failed")
        seed(self.cache_mgr, records)
        aged = time.time() - 90
        markers = {}
        quiet = ["w1:pUndelivered", "w1:pSalvagedDelivered", "w1:pEndedDelivered"]
        for pane in [f"w1:p{s}" for s in states] + quiet:
            marker = pane_file(self.state_dir, pane)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("1")
            os.utime(marker, (aged, aged))
            markers[pane] = marker
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        for state in states:
            self.assertGreater(markers[f"w1:p{state}"].stat().st_mtime, aged + 60, state)
        for pane in quiet:
            self.assertLess(markers[pane].stat().st_mtime, aged + 1, pane)

    def test_heartbeat_stops_when_herdr_dies(self):
        sid = self.sid("w1:pDead")
        seed(self.cache_mgr, {sid: session("w1:pDead", now=self.clock.time())})
        marker = pane_file(self.state_dir, "w1:pDead")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1")
        aged = time.time() - 90
        os.utime(marker, (aged, aged))
        self.clear_fake_processes()
        self.add_fake_process("Bartender 6", pid=424200)
        process.reset_caches()
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        self.assertLess(marker.stat().st_mtime, aged + 1)

    def test_heartbeat_never_clears_failed(self):
        sid = self.sid("w1:pFailed")
        seed(self.cache_mgr, {sid: session("w1:pFailed", now=self.clock.time())})
        failed = pane_file(self.state_dir, "w1:pFailed", ".failed")
        failed.parent.mkdir(parents=True, exist_ok=True)
        failed.write_text("1")
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        self.assertTrue(failed.exists())
        self.assertFalse(pane_file(self.state_dir, "w1:pFailed").exists())


class DrainTests(LoopCase):
    def _comp(self, sid, generation):
        return {"session_id": sid, "pane_id": "w1:pComp", "agent": "Herdr", "generation": generation,
                "admitted_at_ns": 1, "timestamp": self.clock.time()}

    def _seed_comp(self, entry, sessions=None):
        seed(self.cache_mgr, sessions or {}, pending_compensations=[entry])

    def test_p54_plain_compensation_is_sent_then_cleared(self):
        """Plan §10.1 #54 (gap t54-drain-reverify): the reconciler loop sends the persisted Ended and clears it."""
        sid = self.sid("w1:pComp")
        self._seed_comp(self._comp(sid, 1))
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        self.assertEqual([e["state"] for e in self.bridge.events_for(sid)], ["Ended"])
        self.assertEqual(read_cache(self.cache_mgr)["pending_compensations"], [])

    def test_p54_readmitted_target_aborts_without_sending(self):
        """Plan §10.1 #54: under-lock re-verification finds a fresh live session - no Ended, entry purged."""
        sid = self.sid("w1:pComp")
        self._seed_comp(self._comp(sid, 1), {sid: session("w1:pComp", now=self.clock.time(), generation=2)})
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        self.assertEqual(self.bridge.events_for(sid), [])
        self.assertEqual(read_cache(self.cache_mgr)["pending_compensations"], [])

    def test_p54_readmission_raced_during_the_send_forces_resync(self):
        """Plan §10.1 #54: a session admitted while the compensating Ended is in flight is re-synced."""
        sid = self.sid("w1:pComp")
        self._seed_comp(self._comp(sid, 1))

        def admit(payload):
            if payload.get("state") == "Ended":
                self.bridge.on_post = None
                seed(self.cache_mgr, {sid: session("w1:pComp", now=self.clock.time(), generation=2)})

        self.bridge.on_post = admit
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        record = self.sessions()[sid]
        self.assertEqual(record["resync_generation"], 1, "the landed Ended forced a re-sync")
        self.assertEqual([e["state"] for e in self.bridge.events_for(sid)], ["Ended", "Working"],
                         "the re-admitted state was re-asserted after the Ended")
        self.assertEqual((record["delivery_status"], self.bridge.sessions[sid]["state"]), ("delivered", "Working"))

    def test_p54_vendor_cleanup_is_never_dropped_on_a_failed_send(self):
        """Gap compensation-drain-loses-side-effects: an unconfirmed vendor dismissal stays queued (attempt counted)."""
        uuid = "vendor-uuid-drain-0001"
        vendor = pane_file(self.state_dir, "w1:pClean", ".vendor_active")
        vendor.parent.mkdir(parents=True, exist_ok=True)
        vendor.write_text(json.dumps({"vendor_session_id": uuid}))
        seed(self.cache_mgr, {}, pending_vendor_cleanups=[{"pane_id": "w1:pClean", "is_pane_closed": True,
                                                           "timestamp": self.clock.time()}])
        self.bridge.return_code = 500
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        data = read_cache(self.cache_mgr)
        self.assertEqual(data["pending_vendor_cleanups"], [])
        self.assertEqual(data["dismissed_vendor_uuids"][uuid]["attempts"], 1)
        self.assertFalse(vendor.exists())

    def test_p64_reconciler_ended_preserves_step_a_origins(self):
        """Plan §10.1 #64: a reconciler-confirmed container Ended tombstones with the persisted Step A origins."""
        pane = "w1:pOriginTest"
        sid = self.sid(pane)
        origin_ns = time.time_ns() - 5_000_000_000
        seed(self.cache_mgr, {sid: session(pane, "Ended", seq=2, delivered=False, now=self.clock.time(),
                                           close_kind="container", closed_at_ns=origin_ns,
                                           closed_source_ts=12345.67, last_source_timestamp=12340.0)})
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        data = read_cache(self.cache_mgr)
        self.assertNotIn(sid, data["sessions"])
        self.assertEqual(data["tombstones"][pane], {"closed_at_ns": origin_ns, "closed_source_ts": 12345.67,
                                                    "last_source_timestamp": 12340.0})

    def test_p15_waiting_survives_25h_while_idle_expires(self):
        """Plan §10.1 #15: a 25h-old Waiting session survives while a 25h-old Idle session is ended and evicted."""
        now = self.clock.time()
        seed(self.cache_mgr, {self.sid("w1:pWait"): session("w1:pWait", "Waiting", now=now - 90000),
                              self.sid("w1:pIdle"): session("w1:pIdle", "Idle", now=now - 90000)})
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        sessions = self.sessions()
        self.assertEqual(sessions[self.sid("w1:pWait")]["desired_state"], "Waiting")
        self.assertNotIn(self.sid("w1:pIdle"), sessions)


class HousekeepingTests(LoopCase):
    def test_guard_stdin_captures_older_than_60s_are_swept(self):
        stale, fresh = self.state_dir / ".guard_stdin.old", self.state_dir / ".guard_stdin.new"
        stale.write_text("x")
        fresh.write_text("y")
        old = self.clock.time() - 61
        os.utime(stale, (old, old))
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())

    def test_log_rotates_at_1mb(self):
        """Plan §5.1 item 12: plugin.log rotates to plugin.log.1 past 1MB (2MB footprint at most)."""
        log_file = self.state_dir / log.LOG_FILE_NAME
        log_file.write_bytes(b"x" * (log.LOG_MAX_BYTES + 1))
        log.log_debug("after rotation")
        self.assertEqual((self.state_dir / log.ROTATED_LOG_FILE_NAME).stat().st_size, log.LOG_MAX_BYTES + 1)
        self.assertLess(log_file.stat().st_size, 200)
        self.assertEqual(log_file.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
