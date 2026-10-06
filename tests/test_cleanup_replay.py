"""--cleanup (Plan §5.2, §9.1 step 3) and --replay-orphans (Plan §9.2) contracts.

Gaps: cleanup-not-universal-sender, cleanup-failure-state, orphan-replay-lock-and-skip, replay-marker-resync, t14.
"""

import fcntl
import json
import os
import unittest
from unittest import mock

from herdr_bartender import cache, cleanup
from herdr_bartender.cleanup import EXIT_FATAL, EXIT_OK, EXIT_UNCONFIRMED, run_cleanup
from herdr_bartender.markers import touch_pane_marker
from herdr_bartender.orphans import _lock_path, export_orphan_record
from herdr_bartender.paths import get_orphan_path
from herdr_bartender.replay import run_replay_orphans
from herdr_bartender.sender import cleanup_budget, step_b
from tests.support import SandboxTestCase
from tests.support.lock_holder import hold_lock
from tests.support.reconciler_fixtures import pane_file, read_cache, salvaged, seed, session


class CleanupCase(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.clock = self.use_fake_clock()
        now = self.clock.time()
        self.live, self.quiet = self.sid("w1:pC1"), self.sid("w1:pC2")
        seed(self.cache_mgr, {self.live: session("w1:pC1", "Working", seq=2, now=now),
                              self.quiet: salvaged("w1:pC2", now=now)})
        touch_pane_marker("w1:pC1")
        (self.state_dir / "DISABLED").touch()   # the rollback sets DISABLED before --cleanup

    def orphans(self):
        path = get_orphan_path()
        return json.loads(path.read_text())["sessions"] if path.exists() else {}


class CleanupTests(CleanupCase):
    def test_p14_cleanup_bypasses_disabled_and_ends_every_session(self):
        """Plan §10.1 #14 / §5.2: with DISABLED present --cleanup sends Ended for every session (salvaged included)
        through the Universal Sender (each POST under a claimed lease), evicts them, removes markers, exits 0."""
        leased = []
        self.bridge.on_post = lambda p: leased.append(bool(read_cache(self.cache_mgr)["sessions"]
                                                           .get(p["session_id"], {}).get("lease_token")))
        self.assertEqual(run_cleanup(bridge_url=self.mock_url), EXIT_OK)
        self.assertEqual(sorted(e["session_id"] for e in self.bridge.history if e["state"] == "Ended"),
                         sorted([self.live, self.quiet]))
        self.assertEqual(leased, [True, True], "every Ended was sent under a claimed lease (Step A/B/C)")
        self.assertEqual(read_cache(self.cache_mgr)["sessions"], {})
        self.assertFalse(pane_file(self.state_dir, "w1:pC1").exists())
        self.assertTrue((self.state_dir / "DISABLED").exists(), "--cleanup never clears DISABLED")

    def test_unreachable_bridge_exits_2_and_records_failure_state(self):
        """Gap cleanup-failure-state: every unconfirmed session keeps orphaned_ended / delivery_status /
        delivery_error in the cache and is exported to the 0600 orphan file; exit 2."""
        self.bridge.return_code = 500
        self.assertEqual(run_cleanup(bridge_url=self.mock_url), EXIT_UNCONFIRMED)
        sessions = read_cache(self.cache_mgr)["sessions"]
        for sid in (self.live, self.quiet):
            self.assertEqual((sessions[sid]["desired_state"], sessions[sid]["orphaned_ended"],
                              sessions[sid]["delivery_status"]), ("Ended", True, "retryable_exhausted"))
            self.assertEqual(sessions[sid]["delivery_error"], "5xx_server_error")
            self.assertEqual(sessions[sid]["orphaned_at"], self.clock.time(), "the R12 horizon origin")
            self.assertTrue(self.orphans()[sid]["orphaned_ended"])
        self.assertEqual(get_orphan_path().stat().st_mode & 0o777, 0o600)

    def test_rejected_ended_exits_2_and_keeps_the_rejection(self):
        self.bridge.return_code = 400
        self.assertEqual(run_cleanup(bridge_url=self.mock_url), EXIT_UNCONFIRMED)
        record = read_cache(self.cache_mgr)["sessions"][self.live]
        self.assertEqual((record["delivery_status"], record["delivery_error"]),
                         ("non_retryable_failed", "4xx_client_error"))
        self.assertIn(self.live, self.orphans())

    def test_seq_advanced_during_the_send_is_not_cleared(self):
        """Gap cleanup-failure-state: a confirmed Ended whose session moved on meanwhile is not counted as cleared."""
        def advance(payload):
            if payload.get("session_id") == self.live:
                with self.cache_mgr as data:
                    data["sessions"][self.live]["seq"] += 1
                    self.cache_mgr.save(data)

        self.bridge.on_post = advance
        self.assertEqual(run_cleanup(bridge_url=self.mock_url), EXIT_UNCONFIRMED)
        self.assertIn(self.live, self.orphans())

    def test_live_readmission_during_cleanup_is_left_live_and_not_exported(self):
        """A session re-admitted live while --cleanup runs (no DISABLED: a manual run) and one admitted afresh are not
        marked orphaned_ended nor exported (their heartbeat and delivery state stay intact); the exit code is still
        2 because the cache is not empty."""
        (self.state_dir / "DISABLED").unlink()
        fresh = self.sid("w1:pFresh")

        def readmit(payload):
            if payload.get("session_id") != self.live:
                return
            with self.cache_mgr as data:
                record = data["sessions"][self.live]
                seq = record["seq"] + 1
                record.update({"desired_state": "Working", "seq": seq, "delivered_seq": seq,
                               "delivered_state": "Working", "delivery_status": "delivered"})
                data["sessions"][fresh] = session("w1:pFresh", now=self.clock.time())
                self.cache_mgr.save(data)

        self.bridge.on_post = readmit
        self.assertEqual(run_cleanup(bridge_url=self.mock_url), EXIT_UNCONFIRMED)
        sessions = read_cache(self.cache_mgr)["sessions"]
        for sid in (self.live, fresh):
            self.assertEqual(sessions[sid]["delivery_status"], "delivered", sid)
            self.assertNotIn("orphaned_ended", sessions[sid])
            self.assertNotIn(sid, self.orphans())
        self.assertNotIn(self.quiet, sessions, "the salvaged session's Ended was confirmed")

    def test_socket_timeout_and_budget(self):
        """Plan §5.2: 0.15s per session, budget max(10.0, n * 0.15)."""
        timeouts = []
        real = step_b.send_event

        def send(payload, timeout, bridge_url=None):
            timeouts.append(timeout)
            return real(payload, timeout=timeout, bridge_url=bridge_url)

        with mock.patch.object(step_b, "send_event", side_effect=send):
            self.assertEqual(run_cleanup(bridge_url=self.mock_url), EXIT_OK)
        self.assertEqual(timeouts, [0.15, 0.15])
        self.assertEqual((cleanup_budget(3), cleanup_budget(100)), (10.0, 15.0))

    def test_live_lease_is_waited_out_within_the_budget(self):
        """Truth-table rows 4/5 in --cleanup: a live holder defers the session until deadline + 0.5s, then it is ended."""
        holder = self.add_fake_process("event-sender", live=True)
        with self.cache_mgr as data:
            data["sessions"][self.live].update({"sending_pid": holder, "lease_deadline": self.clock.time() + 2.0,
                                                "lease_token": f"{holder}:None:1.0:{self.live}"})
            self.cache_mgr.save(data)
        self.assertEqual(run_cleanup(bridge_url=self.mock_url), EXIT_OK)

    def test_lease_held_past_the_budget_exits_2(self):
        holder = self.add_fake_process("event-sender", live=True)
        with self.cache_mgr as data:
            data["sessions"][self.live].update({"sending_pid": holder, "lease_deadline": self.clock.time() + 100,
                                                "lease_token": f"{holder}:None:1.0:{self.live}"})
            self.cache_mgr.save(data)
        self.assertEqual(run_cleanup(bridge_url=self.mock_url), EXIT_UNCONFIRMED)
        self.assertIn(self.live, self.orphans())
        self.assertEqual(read_cache(self.cache_mgr)["sessions"][self.live]["delivery_error"],
                         cleanup.CLEANUP_UNCONFIRMED_ERROR)

    def test_budget_exhaustion_stops_sending_and_exports_the_rest(self):
        """Gap cleanup-budget: once max(10, n*0.15) seconds are spent no further POST is made; the unsent sessions are
        exported and the exit code is 2."""
        def slow(_payload):
            self.clock.advance(6.0)   # a slow bridge: two answers spend the 10s budget

        self.bridge.on_post = slow
        extra = {self.sid(f"w1:pX{i}"): session(f"w1:pX{i}", now=self.clock.time()) for i in range(2)}
        seed(self.cache_mgr, extra)
        self.assertEqual(run_cleanup(bridge_url=self.mock_url), EXIT_UNCONFIRMED)
        self.assertEqual(len(self.bridge.history), 2)
        self.assertEqual(len(self.orphans()), 2, "the two sessions never sent are exported")

    def test_flushes_the_orphan_journal(self):
        holder = hold_lock(self, _lock_path(get_orphan_path()))
        self.assertIs(export_orphan_record(self.sid("w1:pJ"), {"agent": "Herdr", "pane_id": "w1:pJ"}), False)
        holder.release()
        self.assertEqual(run_cleanup(bridge_url=self.mock_url), EXIT_OK)
        self.assertIn(self.sid("w1:pJ"), self.orphans())

    def test_fatal_error_exits_1(self):
        with mock.patch.object(cleanup, "_stage_all_ended", side_effect=RuntimeError("boom")):
            self.assertEqual(run_cleanup(bridge_url=self.mock_url), EXIT_FATAL)


class ReplayCase(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.path = get_orphan_path()

    def write(self, records):
        self.path.write_text(json.dumps({"version": 1, "sessions": records}))

    def remaining(self):
        return json.loads(self.path.read_text())["sessions"] if self.path.exists() else {}

    def replay(self):
        return run_replay_orphans(str(self.path), bridge_url=self.mock_url, quiet=True)


class ReplayTests(ReplayCase):
    def test_orphan_lock_is_not_held_across_the_post(self):
        """Gap orphan-replay-lock-and-skip: the orphan lock is free while the Ended is in flight."""
        sid = self.sid("w1:pR")
        self.write({sid: {"agent": "Claude (Herdr)", "pane_id": "w1:pR"}})
        free = []

        def probe(_payload):
            fd = os.open(str(_lock_path(self.path)), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                free.append(True)
            except BlockingIOError:
                free.append(False)
            finally:
                os.close(fd)

        self.bridge.on_post = probe
        self.assertIs(self.replay(), True)
        self.assertEqual(free, [True])
        self.assertFalse(self.path.exists())

    def test_journal_only_is_replayed_when_the_file_is_absent(self):
        sid = self.sid("w1:pJ")
        holder = hold_lock(self, _lock_path(self.path))
        export_orphan_record(sid, {"agent": "Herdr", "pane_id": "w1:pJ"})
        holder.release()
        self.assertFalse(self.path.exists())
        self.assertIs(self.replay(), True)
        self.assertEqual([e["state"] for e in self.bridge.events_for(sid)], ["Ended"])

    def test_bounded_to_256_records(self):
        """Gap replay-256-bound: at most 256 records (the newest exports) per run; the rest stay in the file."""
        records = {self.sid(f"w1:p{i}"): {"agent": "Herdr"} for i in range(256 + 4)}
        self.write(records)
        self.assertIs(self.replay(), False, "records beyond the bound remain for the next run")
        self.assertEqual(len(self.bridge.history), 256)
        self.assertEqual(sorted(self.remaining()), sorted(self.sid(f"w1:p{i}") for i in range(4)))
        self.assertIs(self.replay(), True)
        self.assertFalse(self.path.exists())

    def test_invalid_ids_dropped_skips_popped_salvaged_never_suppresses(self):
        """Plan §9.2 items 2-3: invalid ids and skipped records leave the file; a salvaged session never suppresses
        the replay and is never flipped out of its quiescent state (gap replay-marker-resync)."""
        live, quiet = self.sid("w1:pLive"), self.sid("w1:pQuiet")
        seed(self.cache_mgr, {live: session("w1:pLive", now=1.0), quiet: salvaged("w1:pQuiet", now=1.0)})
        touch_pane_marker("w1:pLive")
        self.write({"bad id!": {}, live: {"pane_id": "w1:pLive"}, quiet: {"pane_id": "w1:pQuiet"}})
        self.assertIs(self.replay(), True)
        self.assertEqual([e["session_id"] for e in self.bridge.history], [quiet])
        self.assertFalse(self.path.exists())
        sessions = read_cache(self.cache_mgr)["sessions"]
        self.assertEqual((sessions[quiet]["salvaged"], sessions[quiet]["delivery_status"]), (True, "salvaged"))
        self.assertEqual(sessions[live]["delivery_status"], "delivered")
        self.assertTrue(pane_file(self.state_dir, "w1:pLive").exists(), "a live pane keeps its marker")

    def test_rejection_retries_minimal_payload_then_retains(self):
        sid = self.sid("w1:pRej")
        self.write({sid: {"agent": "Claude (Herdr)", "pane_id": "w1:pRej"}})
        self.bridge.enqueue(400)
        self.bridge.enqueue(400)
        self.assertIs(self.replay(), False)
        self.assertEqual([p["agent"] for p in self.bridge.posts()], ["Claude (Herdr)", "Herdr"])
        self.assertIn(sid, self.remaining())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_post_send_resync_of_a_session_admitted_in_flight(self):
        """Plan §9.2 item 4a: a live session admitted while the orphan Ended is in flight is forced to re-sync."""
        sid = self.sid("w1:pRace")
        self.write({sid: {"agent": "Herdr", "pane_id": "w1:pRace"}})

        def admit(_payload):
            seed(self.cache_mgr, {sid: session("w1:pRace", now=1.0)})

        self.bridge.on_post = admit
        self.assertIs(self.replay(), True)
        record = read_cache(self.cache_mgr)["sessions"][sid]
        self.assertEqual((record["delivered_seq"], record["delivery_status"], record["resync_generation"]),
                         (0, "in_flight", 1))
        self.assertTrue((self.state_dir / "reconciler.pending").exists())
        self.assertTrue(self.spawner.calls, "the reconciler is ensured to re-assert the live state")

    def test_unsaved_post_send_resync_keeps_the_record_until_it_is_saved(self):
        """Plan §9.2 item 4a (gate finding, review round 2): the Ended landed but the re-sync of a session admitted
        while it was in flight could not be saved (CacheError). Dropping the orphan lost that re-sync for good (the
        live record still reads delivered, so nothing re-sends it). The record is kept, stamped ``resync_owed``; the
        next replay re-runs only the re-sync check (no second Ended to the live session) and then drops it."""
        sid = self.sid("w1:pRace")
        self.write({sid: {"agent": "Herdr", "pane_id": "w1:pRace"}})
        admitted = []

        def admit(_payload):
            seed(self.cache_mgr, {sid: session("w1:pRace", now=1.0)})
            admitted.append(True)

        real_save = cache.BoundedSessionCache.save

        def save(mgr, data, *args, **kwargs):
            if admitted:
                raise cache.CacheWriteError("simulated: disk full")
            return real_save(mgr, data, *args, **kwargs)

        self.bridge.on_post = admit
        with mock.patch.object(cache.BoundedSessionCache, "save", autospec=True, side_effect=save):
            self.assertIs(self.replay(), False, "the re-sync is not saved yet: the replay is not done")
        self.assertIs(self.remaining()[sid].get("resync_owed"), True)
        record = read_cache(self.cache_mgr)["sessions"][sid]
        self.assertEqual(record["delivered_seq"], record["seq"], "control: the re-sync really was not saved")
        self.bridge.on_post = None
        self.assertIs(self.replay(), True)
        record = read_cache(self.cache_mgr)["sessions"][sid]
        self.assertEqual((record["delivered_seq"], record["delivery_status"], record["resync_generation"]),
                         (0, "in_flight", 1))
        self.assertEqual(self.remaining(), {})
        self.assertEqual(len(self.bridge.posts()), 1, "the live session is never sent a second Ended")

    def test_cached_ended_is_confirmed_and_evicted(self):
        sid = self.sid("w1:pEnded")
        seed(self.cache_mgr, {sid: session("w1:pEnded", "Ended", seq=3, delivered=False, now=1.0,
                                           orphaned_ended=True, delivery_status="non_retryable_failed")})
        self.write({sid: {"agent": "Herdr", "pane_id": "w1:pEnded"}})
        self.assertIs(self.replay(), True)
        self.assertNotIn(sid, read_cache(self.cache_mgr)["sessions"])

    def test_concurrent_export_survives_the_commit(self):
        first, second = self.sid("w1:pA"), self.sid("w1:pB")
        self.write({first: {"agent": "Herdr"}})
        self.bridge.on_post = lambda _p: export_orphan_record(second, {"agent": "Herdr"})
        self.assertIs(self.replay(), True)
        self.assertEqual(list(self.remaining()), [second])

    def test_disabled_and_state_dir_are_never_touched(self):
        (self.state_dir / "DISABLED").touch()
        sid = self.sid("w1:pD")
        self.write({sid: {"agent": "Herdr"}})
        self.assertIs(self.replay(), True)
        self.assertTrue((self.state_dir / "DISABLED").exists())

    def test_unreadable_file_is_left_untouched(self):
        for content in ("{not json", json.dumps({"sessions": ["x"]})):
            with self.subTest(content=content):
                self.path.write_text(content)
                self.assertIs(self.replay(), False)
                self.assertEqual(self.path.read_text(), content)
                self.assertEqual(self.bridge.history, [])

    def test_missing_file_is_reported(self):
        self.assertIs(self.replay(), False)

    def test_contended_orphan_lock_fails_cleanly(self):
        self.write({self.sid("w1:pL"): {"agent": "Herdr"}})
        hold_lock(self, _lock_path(self.path))
        self.use_fake_clock()   # the bounded 5s wait elapses on the fake clock
        self.assertIs(self.replay(), False)
        self.assertEqual(self.bridge.history, [])


class ReplayMarkerTests(ReplayCase):
    """Round-2 low finding (replay.py:221): after a confirmed orphan Ended the pane marker is removed exactly when no
    live (non-Ended, non-salvaged) session remains on that pane (``_live_on_pane``); other panes are untouched."""

    def _replay(self, pane, cached=None, on_post=None):
        sid = self.sid(pane)
        if cached:
            seed(self.cache_mgr, cached)
        touch_pane_marker(pane)
        self.write({sid: {"agent": "Herdr", "pane_id": pane}})
        self.bridge.on_post = on_post
        self.assertIs(self.replay(), True)
        return pane_file(self.state_dir, pane).exists()

    def test_marker_removed_when_nothing_live_remains_on_the_pane(self):
        other = "w1:pOtherLive"
        cases = {
            "nothing-cached": lambda pane: None,
            "cached-orphaned-ended": lambda pane: {self.sid(pane): session(
                pane, "Ended", seq=3, delivered=False, now=1.0, orphaned_ended=True,
                delivery_status="non_retryable_failed")},
            "salvaged-on-the-pane": lambda pane: {f"{self.sid(pane)}-old": salvaged(pane, now=1.0)},
            "ended-on-the-pane": lambda pane: {f"{self.sid(pane)}-old": session(pane, "Ended", now=1.0)},
            "live-on-another-pane": lambda pane: {self.sid(other): session(other, now=1.0)},
        }
        for label, cached in cases.items():
            with self.subTest(case=label):
                pane = f"w1:pMark{label.replace('-', '')}"
                touch_pane_marker(other)
                self.assertFalse(self._replay(pane, cached(pane)), f"{label}: the marker must be removed")
                self.assertTrue(pane_file(self.state_dir, other).exists(), "another pane's marker is untouched")

    def test_marker_kept_for_a_session_admitted_while_the_ended_was_in_flight(self):
        pane = "w1:pMarkReadmit"

        def admit(_payload):
            seed(self.cache_mgr, {self.sid(pane): session(pane, now=1.0)})

        self.assertTrue(self._replay(pane, on_post=admit), "the re-admitted live session keeps its marker")
        self.assertEqual(read_cache(self.cache_mgr)["sessions"][self.sid(pane)]["delivery_status"], "in_flight")


class CliContractTests(ReplayCase):
    """The same contracts through bin/herdr-bartender as rollback.sh runs them (subprocesses, sandboxed)."""

    def test_cleanup_and_replay_exit_codes(self):
        sid = self.sid("w1:pCli")
        seed(self.cache_mgr, {sid: session("w1:pCli", now=1.0)})
        (self.state_dir / "DISABLED").touch()
        self.bridge.return_code = 500
        self.assertEqual(self.run_cli("--cleanup").returncode, EXIT_UNCONFIRMED)
        self.assertIn(sid, self.remaining())
        self.assertEqual(self.run_cli("--replay-orphans", str(self.path)).returncode, 1, "still unconfirmed")
        self.bridge.return_code = 200
        self.assertEqual(self.run_cli("--replay-orphans", str(self.path)).returncode, 0)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.run_cli("--cleanup").returncode, EXIT_OK)
        self.assertEqual(read_cache(self.cache_mgr)["sessions"], {})
        self.assertTrue((self.state_dir / "DISABLED").exists())


class ReconcilerAutoReplayTests(ReplayCase):
    def test_p47_replayed_only_once_health_is_ok(self):
        """Plan §10.1 #47: the loop replays the orphan file only when /health succeeds."""
        from herdr_bartender.background import run_reconcile_background
        sid = self.sid("wAuto:pOrphan")
        self.bridge.sessions[sid] = {"state": "Working"}
        self.write({sid: {"agent": "Herdr", "pane_id": "wAuto:pOrphan"}})
        self.bridge.health_ok = False
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.bridge.history, [])
        self.bridge.health_ok = True
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        self.assertFalse(self.path.exists())
        self.assertNotIn(sid, self.bridge.sessions)


if __name__ == "__main__":
    unittest.main()
