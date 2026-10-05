"""results/ envelopes (Plan §4.3 Step C lock failure) and their drain (§5.1 item 1a)."""

import json
import os
import re
import stat
import time
import unittest
from unittest import mock

from herdr_bartender import envelopes, results, runtime
from herdr_bartender.cache import CacheWriteError
from herdr_bartender.delivery_state import Outcome, Transmission
from herdr_bartender.envelopes import read_json
from herdr_bartender.orphans import export_orphan_record
from herdr_bartender.paths import get_orphan_path
from herdr_bartender.results import (
    RESULTS_BATCH,
    build_result_envelope,
    drain_results_dir,
    parse_result_envelope,
    write_result_envelope,
)
from herdr_bartender.sanitize import get_hex_pane_id
from tests.support import SandboxTestCase

PANE = "w1:pResult"
TOKEN = "999:1:1.0:sid"
PLAN_FIELDS = {"version", "session_id", "transmitting_seq", "transmitting_state", "status", "error",
               "timestamp_ns", "pid"}


class ResultsTests(SandboxTestCase):
    start_bridge = False

    def setUp(self):
        super().setUp()
        self.sid_ = self.sid(PANE)
        self.results_dir = self.state_dir / "results"

    def _seed(self, **overrides):
        record = {
            "pane_id": PANE, "agent": "Claude (Herdr)", "desired_state": "Working", "seq": 2, "delivered_seq": 1,
            "generation": 3, "admitted_at_ns": 10, "lease_token": TOKEN, "sending_pid": 999,
            "lease_deadline": time.time() + 1.5, "delivery_status": "in_flight", "delivery_attempts": 0,
        }
        record.update(overrides)
        with self.cache_mgr as data:
            data["sessions"][self.sid_] = record
            data["pane_generations"][PANE] = 3
            self.cache_mgr.save(data)

    def _tx(self, state="Working", seq=2):
        return Transmission(self.sid_, PANE, state, seq, "Claude (Herdr)", TOKEN, 0, 3, 10, 5000)

    def _session(self):
        with self.cache_mgr as data:
            return data["sessions"].get(self.sid_), data

    def test_writer_uses_plan_schema_and_name(self):
        """Plan §4.3 L483: results/<timestamp_ns>_<pid>_<seq>.json with version 1 and the plan fields; dir is 0700."""
        path = write_result_envelope(self._tx(), Outcome("success"))
        self.assertRegex(path.name, re.compile(rf"^\d{{20}}_{os.getpid()}_2\.json$"))
        env = json.loads(path.read_text())
        self.assertTrue(PLAN_FIELDS <= set(env), env)
        self.assertEqual((env["version"], env["session_id"], env["transmitting_seq"], env["transmitting_state"],
                          env["status"], env["pid"]), (1, self.sid_, 2, "Working", "success", os.getpid()))
        self.assertEqual((env["pane_id"], env["agent"]), (PANE, "Claude (Herdr)"))
        self.assertEqual(stat.S_IMODE(os.stat(self.results_dir).st_mode), 0o700)
        self.assertEqual(list(self.results_dir.glob("*.tmp")), [])

    def test_envelope_round_trips_every_transmission_field(self):
        """Plan §4.3 L483 (findings r5/r7): write + parse restores the full Transmission snapshot (resync generation,
        generation, admission, arrival, lease) and the outcome, so a drained result acts exactly like Step C."""
        tx = Transmission("herdr:h:w2:pRound", "w2:pRound", "Waiting", 7, "Codex (Herdr)", "tok:9", 3, 11,
                          12345, 67890)
        path = write_result_envelope(tx, Outcome("retryable", "5xx_server_error"))
        parsed_tx, outcome = parse_result_envelope(read_json(path))
        self.assertEqual(parsed_tx, tx)
        self.assertEqual(outcome, Outcome("retryable", "5xx_server_error"))

    def test_parse_rejects_invalid_transmitting_seq(self):
        """Poison results: transmitting_seq must be a non-negative integer (not a bool, string or float)."""
        for bad in (-1, True, "2", 2.0, None):
            with self.subTest(seq=bad):
                env = build_result_envelope(self._tx(), Outcome("success"), 1, 1)
                env["transmitting_seq"] = bad
                with self.assertRaises(ValueError):
                    parse_result_envelope(env)

    def test_defer_result_persists_then_hands_off(self):
        """Plan §4.3 L483 (finding r6): defer_result writes the envelope, flags reconciler.pending and makes
        sure the reconciler runs."""
        with mock.patch.object(results, "ensure_reconciler_running") as spawn:
            path = results.defer_result(self._tx(), Outcome("success"), "lock contended")
        self.assertTrue(path.exists())
        self.assertTrue((self.state_dir / "reconciler.pending").exists())
        spawn.assert_called_once_with()

    def test_defer_result_write_failure_still_hands_off(self):
        """Plan §4.3 L483: even when the envelope cannot be written the reconciler is flagged and started."""
        with mock.patch.object(envelopes, "write_json_atomic", side_effect=OSError(28, "ENOSPC")), \
                mock.patch.object(results, "ensure_reconciler_running") as spawn:
            self.assertIsNone(results.defer_result(self._tx(), Outcome("success"), "lock contended"))
        self.assertTrue((self.state_dir / "reconciler.pending").exists())
        spawn.assert_called_once_with()
        self.assertEqual(list(self.results_dir.glob("*.json")), [])

    def test_backlog_beyond_one_batch_keeps_the_reconciler_flagged(self):
        """§5.1 item 1a (finding: results backlog): a drain that leaves envelopes behind flags reconciler.pending
        so the rest is applied promptly; an emptied directory does not."""
        self._seed(seq=2, lease_token=None, sending_pid=None, lease_deadline=None)
        tx = Transmission(self.sid_, PANE, "Working", 2, "Claude (Herdr)", None, 0, 3, 10, 5000)
        for _ in range(RESULTS_BATCH + 3):
            write_result_envelope(tx, Outcome("success"))
        pending = self.state_dir / "reconciler.pending"
        drain_results_dir(self.state_dir)
        self.assertEqual(len(list(self.results_dir.glob("*.json"))), 3)
        self.assertTrue(pending.exists())
        pending.unlink()
        drain_results_dir(self.state_dir)
        self.assertEqual(list(self.results_dir.glob("*.json")), [])
        self.assertFalse(pending.exists())

    def test_undeletable_result_does_not_keep_the_reconciler_spinning(self):
        """A drained file that could not be unlinked (no backlog) does not re-flag reconciler.pending, so the loop
        cannot spin on it; it is re-applied (as stale) on the next regular pass."""
        self._seed()
        write_result_envelope(self._tx(), Outcome("success"))
        with mock.patch.object(results, "unlink_files"):
            drain_results_dir(self.state_dir)
        self.assertEqual(len(list(self.results_dir.glob("*.json"))), 1)
        self.assertFalse((self.state_dir / "reconciler.pending").exists())

    def test_drained_duplicate_success_does_not_force_resync(self):
        """Finding (re-applied result): a second envelope for an already applied success is consumed as stale."""
        self._seed()
        write_result_envelope(self._tx(), Outcome("success"))
        write_result_envelope(self._tx(), Outcome("success"))
        drain_results_dir(self.state_dir)
        s, _ = self._session()
        self.assertEqual((s["delivered_seq"], s["delivery_status"], s.get("resync_generation", 0)), (2, "delivered", 0))

    def test_drained_failure_at_delivered_seq_is_stale(self):
        """§4.3 L483 (finding d1): a late failure for a seq already confirmed adds no attempt and no .failed."""
        self._seed(delivered_seq=2, delivered_state="Working")
        write_result_envelope(self._tx(), Outcome("non_retryable", "4xx_client_error"))
        drain_results_dir(self.state_dir)
        s, _ = self._session()
        self.assertEqual((s["delivery_status"], s.get("rejected_seq", 0), s["delivery_attempts"]), ("in_flight", 0, 0))
        self.assertFalse((self.state_dir / "panes" / f"{get_hex_pane_id(PANE)}.failed").exists())

    def test_drain_success_matches_step_c(self):
        """§5.1 item 1a: a drained success applies the Step C success branch and clears the lease; file removed."""
        self._seed()
        write_result_envelope(self._tx(), Outcome("success"))
        report = drain_results_dir(self.state_dir)
        self.assertEqual(report.applied, 1)
        s, data = self._session()
        self.assertEqual((s["delivered_seq"], s["delivered_state"], s["delivery_status"]), (2, "Working", "delivered"))
        self.assertIsNone(s["lease_token"])
        self.assertTrue((self.state_dir / "panes" / get_hex_pane_id(PANE)).exists())
        self.assertEqual([c["pane_id"] for c in data["pending_vendor_cleanups"]], [PANE])
        self.assertEqual(list(self.results_dir.glob("*.json")), [])

    def test_drain_ended_success_evicts_with_tombstone_and_orphan_removal(self):
        """Gap results-dir-schema-and-drain: Ended success evicts, re-records the tombstone from persisted origins,
        removes the orphan record (outside the lock) and stages vendor cleanup."""
        closed_at = time.time_ns()
        self._seed(desired_state="Ended", close_kind="container", closed_at_ns=closed_at, closed_source_ts=7.0,
                   last_source_timestamp=6.0)
        export_orphan_record(self.sid_, {"pane_id": PANE}, blocking=True)
        write_result_envelope(self._tx("Ended"), Outcome("success"))
        drain_results_dir(self.state_dir)
        s, data = self._session()
        self.assertIsNone(s)
        self.assertEqual(data["tombstones"][PANE]["closed_at_ns"], closed_at)
        self.assertEqual(data["pending_vendor_cleanups"][0]["is_pane_closed"], True)
        self.assertFalse(get_orphan_path().exists(), "orphan record removed after the save")

    def test_drain_non_retryable_ended_exports_orphan(self):
        """Gap results-dir-schema-and-drain: non_retryable sets rejected_seq + .failed and exports an Ended orphan."""
        self._seed(desired_state="Ended")
        write_result_envelope(self._tx("Ended"), Outcome("non_retryable", "4xx_client_error"))
        drain_results_dir(self.state_dir)
        s, _ = self._session()
        self.assertEqual((s["delivery_status"], s["rejected_seq"], s["delivery_error"]),
                         ("non_retryable_failed", 2, "4xx_client_error"))
        self.assertTrue((self.state_dir / "panes" / f"{get_hex_pane_id(PANE)}.failed").exists())
        orphans = json.loads(get_orphan_path().read_text())["sessions"]
        self.assertIn(self.sid_, orphans)

    def test_drain_retryable_counts_attempt(self):
        """Gap results-dir-schema-and-drain: retryable increments delivery_attempts (5-attempt rule applies)."""
        self._seed(delivery_attempts=4)
        write_result_envelope(self._tx(), Outcome("retryable", "5xx_server_error"))
        drain_results_dir(self.state_dir)
        s, _ = self._session()
        self.assertEqual((s["delivery_attempts"], s["delivery_status"]), (5, "retryable_exhausted"))
        self.assertTrue((self.state_dir / "reconciler.pending").exists())

    def test_drain_ignores_stale_results(self):
        """Plan §4.3 L483: only transmitting_seq >= delivered_seq is applied; stale files are consumed."""
        self._seed(delivered_seq=3, seq=3)
        write_result_envelope(self._tx(seq=2), Outcome("retryable", "network_timeout"))
        report = drain_results_dir(self.state_dir)
        s, _ = self._session()
        self.assertEqual((s["delivery_attempts"], s["delivered_seq"]), (0, 3))
        self.assertEqual(report.applied, 1)
        self.assertEqual(list(self.results_dir.glob("*.json")), [])

    def test_drain_stages_compensation_for_evicted_session(self):
        """Gap results-dir-schema-and-drain: a success for an evicted session stages a compensation carrying the
        real agent and the pane generation."""
        write_result_envelope(self._tx(), Outcome("success"))
        drain_results_dir(self.state_dir)
        _, data = self._session()
        (comp,) = data["pending_compensations"]
        self.assertEqual((comp["session_id"], comp["agent"], comp["generation"]), (self.sid_, "Claude (Herdr)", 3))

    def test_drain_applies_in_fifo_order(self):
        """Plan §5.1 item 1a: strict chronological (filename) order, whatever the write order of seqs."""
        self._seed(seq=4, lease_token=None, sending_pid=None, lease_deadline=None)
        for seq in (3, 2, 4):
            tx = Transmission(self.sid_, PANE, "Working", seq, "Claude (Herdr)", None, 0, 3, 10, 5000)
            write_result_envelope(tx, Outcome("success"))
        seen = []
        real = results.apply_delivery_result

        def spy(data, tx, outcome, **kw):
            seen.append(tx.seq)
            return real(data, tx, outcome, **kw)

        with mock.patch.object(results, "apply_delivery_result", side_effect=spy):
            drain_results_dir(self.state_dir)
        self.assertEqual(seen, [3, 2, 4])
        s, _ = self._session()
        self.assertEqual(s["delivered_seq"], 4)

    def test_parse_rejects_wrong_typed_optional_fields(self):
        """Gate finding (review round 6): optional Transmission fields were copied unchecked, so an object pane_id
        raised TypeError while the result was applied (not quarantined: every pass hit it again)."""
        for field, bad in (("pane_id", {"x": 1}), ("agent", [1]), ("lease_token", 5), ("generation", "3"),
                           ("generation", True), ("admitted_at_ns", [1]), ("arrival_ns", {"a": 1}),
                           ("arrival_ns", -1), ("resync_generation", "x"), ("error", {"e": 1})):
            with self.subTest(field=field, bad=bad):
                env = build_result_envelope(self._tx(), Outcome("success"), 1, 1)
                env[field] = bad
                with self.assertRaises(ValueError):
                    parse_result_envelope(env)
        env = build_result_envelope(self._tx(), Outcome("success"), 1, 1)
        env.update({"pane_id": None, "agent": None, "lease_token": None, "generation": None,
                    "admitted_at_ns": None, "arrival_ns": None, "error": None})
        parse_result_envelope(env)   # control: absent optional fields stay valid

    def test_wrong_typed_result_is_quarantined_and_the_drain_continues(self):
        self._seed()
        self.results_dir.mkdir(parents=True, exist_ok=True)
        env = build_result_envelope(self._tx(), Outcome("success"), 1, 1)
        (self.results_dir / "00000000000000000001_1_2.json").write_text(json.dumps({**env, "pane_id": {"x": 1}}))
        write_result_envelope(self._tx(), Outcome("success"))
        report = drain_results_dir(self.state_dir)
        self.assertEqual((report.applied, report.quarantined), (1, 1))
        self.assertEqual(self._session()[0]["delivered_seq"], 2)

    def test_invalid_result_is_quarantined(self):
        """Poison results never block the drain: they move to results/bad/."""
        self.results_dir.mkdir(parents=True, exist_ok=True)
        (self.results_dir / "00000000000000000001_1_1.json").write_text("{nope")
        (self.results_dir / "00000000000000000002_1_1.json").write_text(json.dumps({"version": 1, "status": "maybe"}))
        report = drain_results_dir(self.state_dir)
        self.assertEqual(report.quarantined, 2)
        self.assertEqual(len(list((self.results_dir / "bad").glob("*.json"))), 2)

    def test_result_kept_when_save_fails(self):
        """Plan §5.1 item 1a: each file is removed only after its mutation is saved."""
        self._seed()
        path = write_result_envelope(self._tx(), Outcome("success"))
        with mock.patch.object(results.BoundedSessionCache, "save", side_effect=CacheWriteError("disk full")):
            with self.assertRaises(CacheWriteError):
                drain_results_dir(self.state_dir)
        self.assertTrue(path.exists())
        s, _ = self._session()
        self.assertEqual(s["delivered_seq"], 1)

    def test_applied_results_are_unlinked_while_the_lock_is_held(self):
        """Plan §5.1 item 1a (finding M42): applied envelopes are removed after the save but BEFORE the cache lock is
        released, so a second drainer (reconciler vs. replay) can never apply the same result twice."""
        self._seed(seq=3, lease_token=None, sending_pid=None, lease_deadline=None)
        paths = [write_result_envelope(Transmission(self.sid_, PANE, "Working", seq, "Claude (Herdr)", None, 0, 3,
                                                    10, 5000), Outcome("success")) for seq in (2, 3)]
        unlinked = []
        real = results.unlink_files

        def spy(batch):
            unlinked.extend((path, runtime.IN_CRITICAL_SECTION) for path in batch)
            return real(batch)

        with mock.patch.object(results, "unlink_files", side_effect=spy):
            self.assertEqual(drain_results_dir(self.state_dir).applied, 2)
        self.assertEqual(sorted(unlinked), sorted((path, True) for path in paths))
        self.assertEqual(list(self.results_dir.glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
