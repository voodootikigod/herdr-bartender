"""Spool: R6 envelopes, cap with close protection, Step A FIFO replay under the lock (Plan §4.3, §10.1 #10/#28)."""

import json
import os
import re
import stat
import time
import unittest
from unittest import mock

from herdr_bartender import envelopes, spool
from herdr_bartender.cache import CacheWriteError
from herdr_bartender.envelopes import ENVELOPE_KEYS
from herdr_bartender.handlers import handle_agent_status_changed, handle_pane_closed, handle_tab_closed
from herdr_bartender.reconciler import reconcile_active_sessions
from herdr_bartender.spool import SPOOL_CAP, enqueue_spool, replay_spool_dir
from tests.support import SandboxTestCase

STATUS = "pane.agent_status_changed"


def env(event_name, event_data, arrival_ns, enqueued_ns=None, **extra):
    record = {"event_name": event_name, "event_data": event_data, "context": {},
              "arrival_ns": arrival_ns, "enqueued_ns": enqueued_ns or arrival_ns}
    record.update(extra)
    return record


class SpoolTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.spool_dir = self.state_dir / "spool"
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self.t0 = time.time_ns()

    def _write(self, name, record):
        path = self.spool_dir / name
        path.write_text(json.dumps(record) if not isinstance(record, str) else record)
        return path

    def _status(self, pane, status, arrival_ns, agent="claude"):
        return env(STATUS, {"pane_id": pane, "workspace_id": pane.split(":")[0], "agent_status": status,
                            "agent": agent}, arrival_ns)

    def _session(self, pane):
        with self.cache_mgr as data:
            return data["sessions"].get(self.sid(pane)), data

    # -- envelope ---------------------------------------------------------------------
    def test_p10_envelope_is_r6_and_named_by_enqueue_time(self):
        """R6 / Plan §4.3 L391: <enqueued_ns:020d>_<pid>_<monotonic_ns>.json holding exactly the R6 keys;
        no generation (unknowable under contention); spool/ is 0700."""
        path = enqueue_spool(STATUS, {"pane_id": "w1:p1", "agent_status": "working"}, {"focused_pane_id": "w1:p1"},
                             arrival_ns=self.t0)
        self.assertRegex(path.name, re.compile(rf"^\d{{20}}_{os.getpid()}_\d+\.json$"))
        record = json.loads(path.read_text())
        self.assertEqual(tuple(sorted(record)), tuple(sorted(ENVELOPE_KEYS)))
        self.assertEqual(record["arrival_ns"], self.t0)
        self.assertGreaterEqual(record["enqueued_ns"], record["arrival_ns"])
        self.assertEqual(int(path.name[:20]), record["enqueued_ns"])
        self.assertEqual(stat.S_IMODE(os.stat(self.spool_dir).st_mode), 0o700)
        self.assertEqual(list(self.spool_dir.glob("*.tmp")), [])

    def test_r1_workspace_frozen_into_colon_less_envelope(self):
        """R1: the enqueuing process's HERDR_WORKSPACE_ID is frozen into a colon-less pane event, so the
        reconciler (another env) replays it onto the same canonical pane."""
        os.environ["HERDR_WORKSPACE_ID"] = "w7"
        path = enqueue_spool(STATUS, {"pane_id": "p1", "agent_status": "working", "agent": "claude"}, {},
                             arrival_ns=self.t0)
        del os.environ["HERDR_WORKSPACE_ID"]
        self.assertEqual(json.loads(path.read_text())["event_data"]["workspace_id"], "w7")
        replay_spool_dir(self.state_dir)
        self.assertIsNotNone(self._session("w7:p1")[0])

    # -- FIFO replay -------------------------------------------------------------------------
    def test_p10_fifo_order_and_supersession(self):
        """Plan §10.1 #10 (gap t10-spool-fifo): envelopes are applied in filename order and an envelope whose
        arrival predates the applied state is dropped as superseded."""
        pane = "w1:pFifo"
        self._write("00000000000000000001_1_1.json", self._status(pane, "working", self.t0 + 1_000))
        self._write("00000000000000000002_1_2.json", self._status(pane, "blocked", self.t0 + 3_000))
        self._write("00000000000000000003_1_3.json", self._status(pane, "idle", self.t0 + 2_000))
        order = []
        real = spool.apply_envelope_locked

        def spy(data, record, alive):
            order.append(record["arrival_ns"] - self.t0)
            return real(data, record, alive)

        with mock.patch.object(spool, "apply_envelope_locked", side_effect=spy):
            batch = replay_spool_dir(self.state_dir)
        self.assertEqual(order, [1_000, 3_000, 2_000], "strict filename (FIFO) order")
        self.assertEqual(len(batch.consumed), 3)
        session, _ = self._session(pane)
        self.assertEqual((session["desired_state"], session["seq"]), ("Waiting", 2), "superseded idle dropped")
        self.assertEqual(list(self.spool_dir.glob("*.json")), [])
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertEqual([e["state"] for e in self.bridge.events_for(self.sid(pane))], ["Waiting"])

    def test_p10_close_admission_ordering(self):
        """Plan §4.3 L398: a spooled close predating the session's admission is discarded; a later one ends it
        and records the tombstone."""
        pane = "w1:pCloseOrder"
        handle_agent_status_changed({"agent_status": "working", "pane_id": pane, "workspace_id": "w1", "agent": "claude"},
                                    {}, bridge_url=self.mock_url, arrival_ns=self.t0)
        self._write("00000000000000000001_1_1.json", env("pane.closed", {"pane_id": pane}, self.t0 - 5_000))
        replay_spool_dir(self.state_dir)
        session, data = self._session(pane)
        self.assertEqual(session["desired_state"], "Working")
        self._write("00000000000000000002_1_1.json", env("pane.closed", {"pane_id": pane}, self.t0 + 5_000))
        replay_spool_dir(self.state_dir)
        session, data = self._session(pane)
        self.assertEqual((session["desired_state"], session["close_kind"]), ("Ended", "container"))
        self.assertEqual(data["tombstones"][pane]["closed_at_ns"], self.t0 + 5_000)

    def test_close_generation_rule_r8(self):
        """R8: skip a close only when session.generation > envelope.generation."""
        pane = "w1:pGenClose"
        handle_agent_status_changed({"agent_status": "working", "pane_id": pane, "workspace_id": "w1", "agent": "claude"},
                                    {}, bridge_url=self.mock_url, arrival_ns=self.t0)
        gen = self._session(pane)[0]["generation"]
        self._write("00000000000000000001_1_1.json", env("pane.closed", {"pane_id": pane}, self.t0 + 1, generation=gen - 1))
        replay_spool_dir(self.state_dir)
        self.assertEqual(self._session(pane)[0]["desired_state"], "Working")
        self._write("00000000000000000002_1_1.json", env("pane.closed", {"pane_id": pane}, self.t0 + 2, generation=gen))
        replay_spool_dir(self.state_dir)
        self.assertEqual(self._session(pane)[0]["desired_state"], "Ended")

    def test_replay_limited_to_16_per_pass(self):
        """Plan §4.3 L394: at most 16 envelopes per pass."""
        for i in range(20):
            self._write(f"{i:020d}_1_1.json", self._status(f"w1:pB{i}", "working", self.t0 + i))
        batch = replay_spool_dir(self.state_dir)
        self.assertEqual(len(batch.consumed), 16)
        self.assertEqual(len(list(self.spool_dir.glob("*.json"))), 4)

    def test_envelopes_unlinked_only_after_save(self):
        """Plan §4.3 L403: a failed save leaves every envelope in place for the next pass."""
        self._write("00000000000000000001_1_1.json", self._status("w1:pSave", "working", self.t0))
        with mock.patch.object(spool.BoundedSessionCache, "save", side_effect=CacheWriteError("disk full")):
            with self.assertRaises(CacheWriteError):
                replay_spool_dir(self.state_dir)
        self.assertEqual(len(list(self.spool_dir.glob("*.json"))), 1)

    # -- poison pills ------------------------------------------------------------------------
    def test_p28_spool_poison_pill_quarantine(self):
        """Plan §10.1 #28: undecodable or schema-invalid envelopes move to spool/bad/; the FIFO continues."""
        self._write("00000000000000000001_1_1.json", "{malformed_json_corrupt")
        self._write("00000000000000000002_1_1.json", env("direct_post", {"state": "Working"}, self.t0))
        self._write("00000000000000000003_1_1.json", {"event_name": STATUS, "event_data": {}, "context": {}})
        for i, generation in enumerate((0, -1, "x", True), start=4):
            self._write(f"{i:020d}_1_1.json", env("pane.closed", {"pane_id": "w1:pGen"}, self.t0, generation=generation))
        self._write("00000000000000000008_1_1.json", self._status("w1:pGood", "working", self.t0))
        batch = replay_spool_dir(self.state_dir)
        self.assertEqual(len(batch.quarantined), 7)
        self.assertEqual(sorted(p.name[:20] for p in (self.spool_dir / "bad").glob("*.json")),
                         [f"{i:020d}" for i in range(1, 8)])
        self.assertIsNotNone(self._session("w1:pGood")[0], "the valid envelope behind the poison pills applies")
        self.assertNotIn("w1:pGen", self._session("w1:pGood")[1]["tombstones"], "bad generations never stage")

    def test_staging_exception_quarantines_envelope_and_fifo_continues(self):
        """Plan §10.1 #28 (finding s5): a schema-valid envelope whose staging raises is quarantined to spool/bad;
        the envelopes behind it still apply and replay never raises."""
        boom = self._write("00000000000000000001_1_1.json", self._status("w1:pBoom", "working", self.t0))
        self._write("00000000000000000002_1_1.json", self._status("w1:pAfter", "working", self.t0 + 1))
        real = spool.apply_envelope_locked

        def apply(data, record, alive):
            if record["event_data"]["pane_id"] == "w1:pBoom":
                raise KeyError("staging bug")
            return real(data, record, alive)

        with mock.patch.object(spool, "apply_envelope_locked", side_effect=apply):
            batch = replay_spool_dir(self.state_dir)
        self.assertEqual(batch.quarantined, (boom,))
        self.assertTrue((self.spool_dir / "bad" / boom.name).exists())
        self.assertIsNotNone(self._session("w1:pAfter")[0])
        self.assertIsNone(self._session("w1:pBoom")[0])
        self.assertEqual(list(self.spool_dir.glob("*.json")), [])

    def test_replay_supersession_gate_uses_last_event_ns(self):
        """Plan §4.3 spool replay (finding s8): without a source timestamp a spooled status older than the session's
        last_event_ns is dropped as superseded, even when it is newer than last_arrival_ns (R4 clamp)."""
        pane = "w1:pClamp"
        handle_agent_status_changed({"agent_status": "working", "pane_id": pane, "workspace_id": "w1", "agent": "claude"},
                                    {}, bridge_url=self.mock_url, arrival_ns=self.t0)
        with self.cache_mgr as data:
            record = data["sessions"][self.sid(pane)]
            record["last_event_ns"] = self.t0 + 10_000  # R4: max(arr_ns, prev + 1)
            record["last_applied_arrival_time"] = self.t0 / 1e9  # arrival ordering alone would accept the envelope
            self.cache_mgr.save(data)
        self._write("00000000000000000001_1_1.json", self._status(pane, "blocked", self.t0 + 5_000))
        batch = replay_spool_dir(self.state_dir)
        self.assertEqual(len(batch.consumed), 1)
        session, _ = self._session(pane)
        self.assertEqual((session["desired_state"], session["seq"]), ("Working", 1), "superseded envelope dropped")

    def test_bad_dir_capped_after_each_quarantine(self):
        """Gap spool-close-protection-agent-exit: spool/bad/ never exceeds 20 files, even within one pass."""
        bad = self.spool_dir / "bad"
        bad.mkdir()
        for i in range(20):
            (bad / f"old{i:02d}.json").write_text("{")
            stamp = time.time() - 1000 + i
            os.utime(bad / f"old{i:02d}.json", (stamp, stamp))
        for i in range(3):
            self._write(f"{i:020d}_1_1.json", "{poison")
        with mock.patch.object(envelopes, "prune_dir", wraps=envelopes.prune_dir) as pruned:
            replay_spool_dir(self.state_dir)
        self.assertEqual(pruned.call_count, 3, "the cap is re-applied after every single quarantine")
        self.assertEqual(len(list(bad.glob("*.json"))), 20)

    # -- cap with close protection ----------------------------------------------------------
    def test_cap_prunes_only_oldest_status_envelopes(self):
        """Plan §4.3 L391 (gap spool-close-protection-agent-exit): at 100 envelopes only the oldest non-close
        envelope is pruned; pane/tab/workspace closes and agent exits are protected."""
        closes = [
            env("pane.closed", {"pane_id": "w1:pC"}, self.t0),
            env("tab.closed", {"tab_id": "w1:t1"}, self.t0),
            env("workspace.closed", {"workspace_id": "w1"}, self.t0),
            self._status("w1:pExit", "idle", self.t0, agent=""),
        ]
        for i, record in enumerate(closes):
            self._write(f"{i:020d}_1_1.json", record)
        for i in range(len(closes), SPOOL_CAP):
            self._write(f"{i:020d}_1_1.json", self._status(f"w1:p{i}", "working", self.t0))
        enqueue_spool(STATUS, {"pane_id": "w1:pNew", "agent_status": "working"}, {}, arrival_ns=self.t0)
        names = sorted(p.name for p in self.spool_dir.glob("*.json"))
        self.assertEqual(len(names), SPOOL_CAP)
        for i in range(len(closes)):
            self.assertIn(f"{i:020d}_1_1.json", names)
        self.assertNotIn(f"{len(closes):020d}_1_1.json", names, "oldest status envelope pruned")

    # -- live Step A replay ------------------------------------------------------------------
    def test_live_step_a_replays_spool_before_the_event(self):
        """Plan §4.3 Step A (gap step-a-spool-replay-missing): a live event drains the spool under its lock
        (no network for replayed sessions), unlinks the envelopes after the save and flags the reconciler."""
        pane_a, pane_b = "w1:pSpoolA", "w1:pSpoolB"
        handle_agent_status_changed({"agent_status": "working", "pane_id": pane_a, "workspace_id": "w1", "agent": "claude"},
                                    {}, bridge_url=self.mock_url, arrival_ns=self.t0)
        posts_before = len(self.bridge.requests)
        self._write("00000000000000000001_1_1.json", env("pane.closed", {"pane_id": pane_a}, self.t0 + 1_000))
        handle_agent_status_changed({"agent_status": "working", "pane_id": pane_b, "workspace_id": "w1", "agent": "claude"},
                                    {}, bridge_url=self.mock_url, arrival_ns=self.t0 + 2_000)
        session_a, _ = self._session(pane_a)
        self.assertEqual((session_a["desired_state"], session_a["seq"]), ("Ended", 2))
        self.assertLess(session_a["delivered_seq"], session_a["seq"], "replayed close is left to the reconciler")
        self.assertEqual(self._session(pane_b)[0]["delivery_status"], "delivered")
        self.assertEqual([r["body"]["session_id"] for r in self.bridge.requests[posts_before:]], [self.sid(pane_b)])
        self.assertEqual(list(self.spool_dir.glob("*.json")), [])
        self.assertTrue((self.state_dir / "reconciler.pending").exists())


class CloseHandlerReplayTests(SandboxTestCase):
    """Plan §4.3 Step A (gap step-a-spool-replay-missing, finding: close paths untested): every close handler
    replays the spool under its lock, FIFO and before the live close, and unlinks only after the save."""

    def setUp(self):
        super().setUp()
        self.spool_dir = self.state_dir / "spool"
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self.t0 = time.time_ns()

    def _spool_status(self, name, pane, status, arrival_ns, tab="w1:t1"):
        (self.spool_dir / name).write_text(json.dumps(env(STATUS, {
            "pane_id": pane, "workspace_id": "w1", "tab_id": tab, "agent_status": status, "agent": "claude"},
            arrival_ns)))

    def _assert_replayed_then_closed(self, close):
        pane, other = "w1:pLiveClose", "w1:pSpoolOnly"
        self._spool_status("00000000000000000001_1_1.json", pane, "working", self.t0 + 1_000)
        self._spool_status("00000000000000000002_1_1.json", pane, "blocked", self.t0 + 2_000)
        self._spool_status("00000000000000000003_1_1.json", other, "working", self.t0 + 3_000, tab="w1:t2")
        order = []
        real_save = spool.BoundedSessionCache.save

        def save(cache_mgr, data, *args, **kwargs):
            order.append(("save", sorted(p.name for p in self.spool_dir.glob("*.json"))))
            return real_save(cache_mgr, data, *args, **kwargs)

        with mock.patch.object(spool.BoundedSessionCache, "save", autospec=True, side_effect=save):
            close(self.t0 + 4_000)
        self.assertEqual(len(order[0][1]), 3, "envelopes still present when the Step A save runs")
        self.assertEqual(list(self.spool_dir.glob("*.json")), [], "unlinked after the save")
        sent = [(r["body"]["session_id"], r["body"]["state"]) for r in self.bridge.requests]
        self.assertEqual(sent, [(self.sid(pane), "Ended")], "only the live close is sent inline")
        with self.cache_mgr as data:
            self.assertNotIn(self.sid(pane), data["sessions"], "replayed Waiting then live Ended, delivered")
            replayed = data["sessions"][self.sid(other)]
        self.assertEqual((replayed["desired_state"], replayed["seq"]), ("Working", 1))
        self.assertLess(replayed.get("delivered_seq", 0), replayed["seq"], "replayed sessions go to the reconciler")

    def test_pane_closed_replays_spool_first(self):
        self._assert_replayed_then_closed(
            lambda ns: handle_pane_closed({"pane_id": "w1:pLiveClose"}, {}, bridge_url=self.mock_url, arrival_ns=ns))

    def test_tab_closed_replays_spool_first(self):
        self._assert_replayed_then_closed(lambda ns: handle_tab_closed(
            {"tab_id": "w1:t1", "workspace_id": "w1"}, {}, bridge_url=self.mock_url, arrival_ns=ns))


if __name__ == "__main__":
    unittest.main()
