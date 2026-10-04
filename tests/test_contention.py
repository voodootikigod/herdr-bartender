"""Event-path lock contention, unsaved state and DISABLED under the lock (Plan §4.3 Step A/C contention rules)."""

import json
import signal
import time
import unittest
from unittest import mock

from herdr_bartender import cache, envelopes, orphans, runtime, spool, watchdog
from herdr_bartender.bridge import DeliveryResult
from herdr_bartender.cache import CacheWriteError
from herdr_bartender.handlers import (
    handle_agent_status_changed,
    handle_pane_closed,
    handle_tab_closed,
    handle_workspace_closed,
)
from herdr_bartender.sender import dispatch, step_b
from herdr_bartender.results import drain_results_dir
from tests.support import SandboxTestCase
from tests.support.lock_holder import hold_lock

STATUS = "pane.agent_status_changed"
WORKING = {"agent_status": "working", "pane_id": "w1:pCont", "workspace_id": "w1", "agent": "claude",
           "tab_id": "w1:t1"}


class ContentionTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.spool_dir = self.state_dir / "spool"
        self.results_dir = self.state_dir / "results"

    def _spooled(self):
        return [json.loads(p.read_text()) for p in sorted(self.spool_dir.glob("*.json"))]

    def _session(self, pane="w1:pCont"):
        with self.cache_mgr as data:
            return data["sessions"].get(self.sid(pane))

    def test_step_a_contention_spools_every_event_kind(self):
        """Plan §4.3 L391 (gaps lock-best-effort, no-contention-spool-path, lock-timeout-proceeds-unlocked):
        with the cache lock held by another PROCESS each handler spools an R6 envelope, writes no cache,
        sends nothing and flags the reconciler."""
        holder = hold_lock(self, self.cache_mgr.lock_file)
        cases = [
            (STATUS, lambda: handle_agent_status_changed(WORKING, {}, bridge_url=self.mock_url, arrival_ns=111)),
            ("pane.closed", lambda: handle_pane_closed({"pane_id": "w1:pCont"}, {}, bridge_url=self.mock_url, arrival_ns=222)),
            ("tab.closed", lambda: handle_tab_closed({"tab_id": "w1:t1"}, {}, bridge_url=self.mock_url, arrival_ns=333)),
            ("workspace.closed", lambda: handle_workspace_closed({"workspace_id": "w1"}, {}, bridge_url=self.mock_url,
                                                                arrival_ns=444)),
        ]
        for name, call in cases:
            with self.subTest(event=name):
                t0 = time.monotonic()
                call()
                self.assertLess(time.monotonic() - t0, 1.0)
        holder.release()
        self.assertEqual([(e["event_name"], e["arrival_ns"]) for e in self._spooled()],
                         [(name, ns) for (name, _), ns in zip(cases, (111, 222, 333, 444))])
        self.assertFalse(self.cache_mgr.cache_file.exists(), "nothing may be written without the lock")
        self.assertEqual(self.bridge.requests, [])
        self.assertTrue((self.state_dir / "reconciler.pending").exists())

    def test_cli_contention_exits_zero_and_spools(self):
        """Plan §4.3 L391 at process level: bin/herdr-bartender under contention exits 0 within the budget."""
        hold_lock(self, self.cache_mgr.lock_file)
        envelope = {"event": STATUS, "data": WORKING, "context": {}}
        t0 = time.monotonic()
        proc = self.run_cli(STATUS, input=json.dumps(envelope))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(time.monotonic() - t0, 2.0)
        (spooled,) = self._spooled()
        self.assertEqual((spooled["event_name"], spooled["event_data"]["pane_id"]), (STATUS, "w1:pCont"))
        self.assertFalse(self.cache_mgr.cache_file.exists())
        self.assertEqual(self.bridge.requests, [])

    def test_read_only_cli_commands_report_contention(self):
        """Gap lock-best-effort: --status/--sessions never read the cache unlocked; under contention they fail
        cleanly (exit 1, message, no traceback)."""
        hold_lock(self, self.cache_mgr.lock_file)
        for flag in ("--status", "--sessions"):
            with self.subTest(flag=flag):
                proc = self.run_cli(flag)
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertIn("Session cache unavailable", proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)

    def test_step_c_contention_writes_result_envelope(self):
        """Plan §4.3 L483 (gaps lock-best-effort, results-dir-schema-and-drain): when Step C cannot re-take the
        lock the outcome goes to results/; the drain later applies it exactly like Step C."""
        holders = []

        def send_then_contend(payload, bridge_url=None, timeout=0.2):
            holders.append(hold_lock(self, self.cache_mgr.lock_file))
            return DeliveryResult("success", None, 200)

        with mock.patch.object(step_b, "send_event", side_effect=send_then_contend):
            handle_agent_status_changed(WORKING, {}, bridge_url=self.mock_url)
        holders[0].release()
        (result_file,) = self.results_dir.glob("*.json")
        result = json.loads(result_file.read_text())
        self.assertEqual((result["session_id"], result["transmitting_seq"], result["transmitting_state"],
                          result["status"], result["version"]), (self.sid("w1:pCont"), 1, "Working", "success", 1))
        self.assertEqual(self._session().get("delivered_seq", 0), 0, "Step C did not run unlocked")
        drain_results_dir(self.state_dir)
        session = self._session()
        self.assertEqual((session["delivered_seq"], session["delivery_status"], session["lease_token"]),
                         (1, "delivered", None))

    def test_close_step_c_contention_writes_result_and_drain_evicts(self):
        """Plan §4.3 L483 for the close path: the deferred Ended confirmation evicts on drain."""
        handle_agent_status_changed(WORKING, {}, bridge_url=self.mock_url)
        holders = []

        def send_then_contend(payload, bridge_url=None, timeout=0.2):
            holders.append(hold_lock(self, self.cache_mgr.lock_file))
            return DeliveryResult("success", None, 200)

        with mock.patch.object(step_b, "send_event", side_effect=send_then_contend):
            handle_pane_closed({"pane_id": "w1:pCont"}, {}, bridge_url=self.mock_url)
        holders[0].release()
        self.assertEqual(self._session()["desired_state"], "Ended")
        drain_results_dir(self.state_dir)
        with self.cache_mgr as data:
            self.assertNotIn(self.sid("w1:pCont"), data["sessions"])
            self.assertIn("w1:pCont", data["tombstones"])

    def test_unsaved_step_a_never_sends(self):
        """Gap save-errors-swallowed: if the Step A save fails, no POST is made and the event is spooled."""
        with mock.patch.object(cache, "write_cache_file", side_effect=CacheWriteError("read-only fs")):
            handle_agent_status_changed(WORKING, {}, bridge_url=self.mock_url)
        self.assertEqual(self.bridge.requests, [])
        self.assertEqual([e["event_name"] for e in self._spooled()], [STATUS])
        self.assertIn("Deferred pane.agent_status_changed", (self.state_dir / "plugin.log").read_text())

    def test_unsaved_step_a_keeps_the_replayed_envelopes(self):
        """Plan §4.3 Step A (gap save-errors-swallowed): envelopes replayed in a status Step A whose save fails are
        not unlinked (unlink only after the save), so the earlier contended event survives next to the new one."""
        earlier = {**WORKING, "pane_id": "w1:pEarlier"}
        earlier_path = spool.enqueue_spool(STATUS, earlier, {}, arrival_ns=100)
        with mock.patch.object(cache, "write_cache_file", side_effect=CacheWriteError("read-only fs")):
            handle_agent_status_changed(WORKING, {}, bridge_url=self.mock_url)
        self.assertTrue(earlier_path.exists(), "a replayed envelope was unlinked although its staging was not saved")
        self.assertEqual([e["event_data"]["pane_id"] for e in self._spooled()], ["w1:pEarlier", "w1:pCont"])
        self.assertEqual(self.bridge.requests, [])

    def test_unsaved_step_c_records_result(self):
        """Gap save-errors-swallowed: a failed Step C save writes the outcome to results/ instead of losing it."""
        real = cache.write_cache_file
        calls = []

        def fail_second(path, data):
            calls.append(path)
            if len(calls) == 2:
                raise CacheWriteError("disk full")
            return real(path, data)

        with mock.patch.object(cache, "write_cache_file", side_effect=fail_second):
            handle_agent_status_changed(WORKING, {}, bridge_url=self.mock_url)
        self.assertEqual(len(self.bridge.requests), 1)
        (result_file,) = self.results_dir.glob("*.json")
        self.assertEqual(json.loads(result_file.read_text())["status"], "success")

    def test_disabled_rechecked_under_lock_in_step_a_and_c(self):
        """Plan §4.3 L392/L484 (gap is-disabled-under-lock): DISABLED observed under the lock aborts without
        saving or sending; appearing during Step B, Step C neither saves nor sends again."""
        (self.state_dir / "DISABLED").touch()
        handle_agent_status_changed(WORKING, {}, bridge_url=self.mock_url)
        self.assertFalse(self.cache_mgr.cache_file.exists())
        self.assertEqual(self.bridge.requests, [])
        (self.state_dir / "DISABLED").unlink()

        def send_then_disable(payload, bridge_url=None, timeout=0.2):
            (self.state_dir / "DISABLED").touch()
            return DeliveryResult("success", None, 200)

        with mock.patch.object(step_b, "send_event", side_effect=send_then_disable) as sent:
            handle_agent_status_changed(WORKING, {}, bridge_url=self.mock_url)
        sent.assert_called_once()
        self.assertEqual(self._session().get("delivered_seq", 0), 0, "Step C must not apply anything while disabled")
        self.assertEqual(list(self.results_dir.glob("*.json")) if self.results_dir.exists() else [], [])


class WatchdogDuringDeferralTests(SandboxTestCase):
    """Plan §4.3 contention rule under R23: a SIGALRM never drops an event that is still being saved or spooled."""

    def setUp(self):
        super().setUp()
        self.spool_dir = self.state_dir / "spool"
        self.results_dir = self.state_dir / "results"

    def _spooled(self):
        return [json.loads(p.read_text()) for p in sorted(self.spool_dir.glob("*.json"))]

    def _alarm_then(self, real, *args, **kwargs):
        watchdog._timeout_watchdog(signal.SIGALRM, None)
        return real(*args, **kwargs)

    def _run_expecting_deadline_exit(self, fn, *args):
        with self.assertRaises(SystemExit) as cm:
            watchdog.run_bounded(fn, *args)
        self.assertEqual(cm.exception.code, 0)
        self.assertTrue((self.state_dir / "reconciler.pending").exists())

    def test_sigalrm_during_lock_wait_still_spools_every_event_kind(self):
        """Finding (watchdog vs contention spool): SIGALRM while waiting for a contended lock defers the exit until
        the event is spooled; then the process hands off and exits 0."""
        cases = [
            (STATUS, handle_agent_status_changed, (WORKING, {}, self.mock_url, None, 111)),
            ("pane.closed", handle_pane_closed, ({"pane_id": "w1:pCont"}, {}, self.mock_url, 222)),
            ("tab.closed", handle_tab_closed, ({"tab_id": "w1:t1"}, {}, self.mock_url, 333)),
            ("workspace.closed", handle_workspace_closed, ({"workspace_id": "w1"}, {}, self.mock_url, 444)),
        ]
        hold_lock(self, self.cache_mgr.lock_file)
        for name, handler, args in cases:
            with self.subTest(event=name):
                runtime.PENDING_WATCHDOG_EXIT = False
                with mock.patch.object(cache, "_flock_within", side_effect=lambda fd, t: self._alarm_then(
                        lambda: False)):
                    self._run_expecting_deadline_exit(handler, *args)
        self.assertEqual([(e["event_name"], e["arrival_ns"]) for e in self._spooled()],
                         [(name, args[-1]) for name, _, args in cases])
        self.assertEqual(self.bridge.requests, [])

    def test_sigalrm_during_spool_write_completes_the_envelope(self):
        """Finding (watchdog vs contention spool): SIGALRM in the middle of writing the envelope lets the atomic
        write finish (no lost close, no stray tmp file)."""
        hold_lock(self, self.cache_mgr.lock_file)
        real = envelopes.write_json_atomic
        with mock.patch("herdr_bartender.spool.write_json_atomic",
                        side_effect=lambda path, obj: self._alarm_then(real, path, obj)):
            self._run_expecting_deadline_exit(handle_pane_closed, {"pane_id": "w1:pCont"}, {}, self.mock_url, 222)
        self.assertEqual([e["event_name"] for e in self._spooled()], ["pane.closed"])
        self.assertEqual(list(self.spool_dir.glob("*.tmp")), [])

    def test_real_itimer_during_contended_lock_wait_spools_the_close(self):
        """Finding (watchdog vs contention spool), with a real ITIMER_REAL: the deadline expires while another
        process holds the cache lock; the pane.closed is still spooled and the process exits 0."""
        hold_lock(self, self.cache_mgr.lock_file)
        previous = signal.getsignal(signal.SIGALRM)
        self.addCleanup(signal.signal, signal.SIGALRM, previous)
        self.addCleanup(watchdog.disarm_watchdog)
        runtime.PROCESS_DEADLINE_SECONDS = 0.05
        runtime.START_TIME = time.monotonic()
        with mock.patch.object(cache, "lock_timeout_seconds", return_value=0.4):
            self.assertTrue(watchdog.arm_watchdog())
            self._run_expecting_deadline_exit(handle_pane_closed, {"pane_id": "w1:pCont"}, {}, self.mock_url, 222)
        self.assertTrue(runtime.PENDING_WATCHDOG_EXIT, "the deadline really fired")
        self.assertEqual([e["event_name"] for e in self._spooled()], ["pane.closed"])

    def test_sigalrm_during_step_c_lock_wait_writes_result(self):
        """Finding (defer_result vs watchdog): SIGALRM while Step C waits for a contended lock still records the
        delivery outcome in results/ before the deadline exit."""
        handle_agent_status_changed(WORKING, {}, bridge_url=self.mock_url)
        real_flock, sent = cache._flock_within, []

        def flock(fd, timeout):
            return self._alarm_then(lambda: False) if sent else real_flock(fd, timeout)

        def send(payload, bridge_url=None, timeout=0.2):
            sent.append(payload)
            return DeliveryResult("success", None, 200)

        with mock.patch.object(step_b, "send_event", side_effect=send), \
                mock.patch.object(cache, "_flock_within", side_effect=flock):
            self._run_expecting_deadline_exit(handle_pane_closed, {"pane_id": "w1:pCont"}, {}, self.mock_url)
        self.assertEqual(len(sent), 1)
        (result_file,) = self.results_dir.glob("*.json")
        self.assertEqual(json.loads(result_file.read_text())["transmitting_state"], "Ended")

    def test_sigalrm_during_status_step_c_lock_wait_writes_result(self):
        """Finding M61 (defer_result vs watchdog, status path): SIGALRM while the status handler's Step C waits for a
        contended lock still records the delivered Working in results/ before the deadline exit."""
        real_flock, sent = cache._flock_within, []

        def flock(fd, timeout):
            return self._alarm_then(lambda: False) if sent else real_flock(fd, timeout)

        def send(payload, bridge_url=None, timeout=0.2):
            sent.append(payload)
            return DeliveryResult("success", None, 200)

        with mock.patch.object(step_b, "send_event", side_effect=send), \
                mock.patch.object(cache, "_flock_within", side_effect=flock):
            self._run_expecting_deadline_exit(handle_agent_status_changed, WORKING, {}, self.mock_url)
        self.assertEqual(len(sent), 1)
        (result_file,) = self.results_dir.glob("*.json")
        result = json.loads(result_file.read_text())
        self.assertEqual((result["transmitting_state"], result["status"]), ("Working", "success"))


class WatchdogAfterStepCTests(SandboxTestCase):
    """Plan §4.3 Persisted Side Effects Guarantee under R23: a deadline that passes while Step C (or the compensation
    re-verify) holds the lock must not drop the orphan export or the owed compensating Ended."""

    def _alarm_on_load(self, armed, nth):
        """Patch the locked load: the ``nth`` load after ``armed`` is set fires SIGALRM inside the critical section."""
        real_load = cache.BoundedSessionCache._load
        loads = []

        def load(mgr):
            data = real_load(mgr)
            if armed:
                loads.append(1)
                if len(loads) == nth:
                    watchdog._timeout_watchdog(signal.SIGALRM, None)
            return data

        return mock.patch.object(cache.BoundedSessionCache, "_load", autospec=True, side_effect=load)

    def _orphaned(self, sid):
        """The orphan export for ``sid`` landed in the orphan file or in its R10 journal."""
        path = orphans.get_orphan_path()
        exported = json.loads(path.read_text())["sessions"] if path.exists() else {}
        journal = orphans.pending_dir_for(path)
        journaled = [json.loads(p.read_text()) for p in journal.glob("*.json")] if journal.is_dir() else []
        return sid in exported or any(op.get("sid") == sid and op.get("op") == "export" for op in journaled)

    def _run(self, fn, *args):
        try:
            watchdog.run_bounded(fn, *args)
        except SystemExit as exc:
            self.assertEqual(exc.code, 0)
        self.assertTrue(runtime.PENDING_WATCHDOG_EXIT, "the deadline fired")
        self.assertTrue((self.state_dir / "reconciler.pending").exists())
        runtime.PENDING_WATCHDOG_EXIT = False  # the process is gone; let the test inspect the cache

    def test_sigalrm_in_close_step_c_still_exports_orphan(self):
        """Finding (orphan I/O after the deferred section, close path): a rejected Ended whose Step C lock hold sees
        the deadline is still mirrored to the orphan file (or journaled) before the process exits."""
        handle_agent_status_changed(WORKING, {}, bridge_url=self.mock_url)
        sid, sent = self.sid("w1:pCont"), []

        def reject(payload, bridge_url=None, timeout=0.2):
            sent.append(payload)
            return DeliveryResult("non_retryable", "4xx_client_error", 400)

        with mock.patch.object(step_b, "send_event", side_effect=reject), self._alarm_on_load(sent, 1):
            self._run(handle_pane_closed, {"pane_id": "w1:pCont"}, {}, self.mock_url)
        with self.cache_mgr as data:
            self.assertIs(data["sessions"][sid]["orphaned_ended"], True)
        self.assertTrue(self._orphaned(sid), "orphaned_ended was saved but never exported")

    def test_sigalrm_in_status_step_c_still_exports_orphan(self):
        """Finding (orphan I/O after the deferred section, status path): an agent-exit Ended rejected by the bridge
        is exported even when the deadline passes during its Step C lock hold."""
        handle_agent_status_changed(WORKING, {}, bridge_url=self.mock_url)
        sid, sent = self.sid("w1:pCont"), []

        def reject(payload, bridge_url=None, timeout=0.2):
            sent.append(payload)
            return DeliveryResult("non_retryable", "4xx_client_error", 400)

        agent_exit = {**WORKING, "agent_status": "idle", "agent": ""}
        with mock.patch.object(step_b, "send_event", side_effect=reject), self._alarm_on_load(sent, 1):
            self._run(handle_agent_status_changed, agent_exit, {}, self.mock_url)
        with self.cache_mgr as data:
            self.assertIs(data["sessions"][sid]["orphaned_ended"], True)
        self.assertTrue(self._orphaned(sid), "orphaned_ended was saved but never exported")

    def _evict_during_send(self, sent, outcome="success"):
        """deliver_event stand-in: a concurrent close confirms and evicts the session while the Working is in flight."""
        def send(payload, bridge_url=None, timeout=0.2):
            with self.cache_mgr as data:
                data["sessions"].pop(payload["session_id"], None)
                data["tombstones"]["w1:pCont"] = {"closed_at_ns": time.time_ns(), "closed_source_ts": time.time(),
                                                  "last_source_timestamp": 0.0}
                self.cache_mgr.save(data)
            sent.append(payload)
            return DeliveryResult(outcome, None if outcome == "success" else "network_timeout",
                                  200 if outcome == "success" else None)
        return send

    def _owed_or_sent(self, sid):
        with self.cache_mgr as data:
            owed = [c for c in data.get("pending_compensations", []) if c.get("session_id") == sid]
        posted = [e for e in self.bridge.events_for(sid) if e.get("state") == "Ended"]
        return owed, posted

    def test_sigalrm_during_compensation_reverify_keeps_the_owed_ended(self):
        """Finding (compensation cleared before its POST): a deadline during the under-lock re-verification never
        leaves the compensating Ended both unsent and unrecorded; it stays in pending_compensations for the
        reconciler."""
        sid, sent = self.sid("w1:pCont"), []
        with mock.patch.object(step_b, "send_event", side_effect=self._evict_during_send(sent)), \
                self._alarm_on_load(sent, 2):
            self._run(handle_agent_status_changed, WORKING, {}, self.mock_url)
        owed, posted = self._owed_or_sent(sid)
        self.assertEqual(len(sent), 1)
        self.assertTrue(owed or posted, "the compensating Ended was neither sent nor kept for the reconciler")

    def test_sigalrm_during_compensating_post_keeps_the_owed_ended(self):
        """Finding (compensation cleared before its POST): a deadline that unwinds the compensating POST leaves the
        persisted compensation in place, so the reconciler still dismisses the phantom."""
        sid, sent = self.sid("w1:pCont"), []

        def expire(*args, **kwargs):
            watchdog._timeout_watchdog(signal.SIGALRM, None)

        with mock.patch.object(step_b, "send_event", side_effect=self._evict_during_send(sent)), \
                mock.patch.object(dispatch, "send_event", side_effect=expire):
            self._run(handle_agent_status_changed, WORKING, {}, self.mock_url)
        owed, _ = self._owed_or_sent(sid)
        self.assertEqual(len(owed), 1, "the compensation was cleared although its POST never completed")

    def test_sigalrm_after_compensating_post_still_detects_readmission(self):
        """Plan §4.3 Post-Compensation Re-Sync Detection under R23: if a session was re-admitted while the
        compensating Ended was in flight and the deadline passes while waiting for the lock, the re-sync is still
        forced (the Ended may have dismissed the new session) and the delivered compensation is cleared."""
        sid, sent, posted = self.sid("w1:pCont"), [], []
        real_flock = cache._flock_within

        def post_while_readmitted(payload, timeout=0.2, bridge_url=None):
            with self.cache_mgr as data:
                data["sessions"][sid] = {"pane_id": "w1:pCont", "desired_state": "Working", "seq": 5,
                                         "delivered_seq": 5, "delivery_status": "delivered"}
                self.cache_mgr.save(data)
            posted.append(payload)
            return DeliveryResult("success", None, 200)

        def flock(fd, timeout):
            return watchdog._timeout_watchdog(signal.SIGALRM, None) or real_flock(fd, timeout) if posted \
                else real_flock(fd, timeout)

        with mock.patch.object(step_b, "send_event", side_effect=self._evict_during_send(sent)), \
                mock.patch.object(dispatch, "send_event", side_effect=post_while_readmitted), \
                mock.patch.object(cache, "_flock_within", side_effect=flock):
            self._run(handle_agent_status_changed, WORKING, {}, self.mock_url)
        self.assertEqual(len(posted), 1)
        with self.cache_mgr as data:
            readmitted = data["sessions"][sid]
            self.assertEqual((readmitted["delivered_seq"], readmitted["delivery_status"]), (0, "in_flight"))
            self.assertEqual(data["pending_compensations"], [])

    def test_failed_compensating_post_stays_owed(self):
        """Plan §4.3 Persisted Side Effects Guarantee: the entry is cleared only after a successful dispatch; a
        compensating Ended the bridge did not accept stays in pending_compensations and the reconciler is flagged."""
        sid, sent = self.sid("w1:pCont"), []
        self.bridge.return_code = 500
        with mock.patch.object(step_b, "send_event", side_effect=self._evict_during_send(sent)):
            handle_agent_status_changed(WORKING, {}, bridge_url=self.mock_url)
        owed, _ = self._owed_or_sent(sid)
        attempts = [r for r in self.bridge.requests if (r["body"] or {}).get("session_id") == sid]
        self.assertEqual([(r["body"]["state"], r["status"]) for r in attempts], [("Ended", 500)])
        self.assertEqual(len(owed), 1, "a compensation the bridge did not accept is still owed")
        self.assertTrue(self.spawner.calls, "the owed compensation is handed to the reconciler")
        self.assertTrue((self.state_dir / "reconciler.pending").exists())

    def test_completed_compensation_is_posted_then_cleared(self):
        """Plan §4.3 Persisted Side Effects Guarantee: without a deadline the compensating Ended is posted and only
        then cleared from pending_compensations."""
        sid, sent = self.sid("w1:pCont"), []
        with mock.patch.object(step_b, "send_event", side_effect=self._evict_during_send(sent)):
            handle_agent_status_changed(WORKING, {}, bridge_url=self.mock_url)
        owed, posted = self._owed_or_sent(sid)
        self.assertEqual((len(owed), len(posted)), (0, 1))
        self.assertEqual(set(posted[0]), {"state", "agent", "session_id"})

    def test_retryable_send_for_evicted_session_is_compensated(self):
        """Finding M63 (inline status Step C vs delivery_state): a retryable outcome may still have landed, so a
        Working sent for a session evicted meanwhile is compensated with an Ended, exactly like the results drain."""
        sid, sent = self.sid("w1:pCont"), []
        with mock.patch.object(step_b, "send_event", side_effect=self._evict_during_send(sent, "retryable")):
            handle_agent_status_changed(WORKING, {}, bridge_url=self.mock_url)
        owed, posted = self._owed_or_sent(sid)
        self.assertEqual(len(sent), 1)
        self.assertEqual((len(owed), len(posted)), (0, 1), "a retryable send must be compensated like a success")


if __name__ == "__main__":
    unittest.main()
