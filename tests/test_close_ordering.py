"""R48: a close and a trailing status give the same outcome whichever process wins the cache lock.

Round-4 finding: Herdr runs every event in its own plugin process and ``arrival_ns`` is captured before the
lock wait, so a ``pane.closed`` arriving at t1 and a trailing status arriving at t1+3ms can be processed in
either order. Close first: the tombstone rejects the trailing non-working status (Plan §4.3 L410-421).
Status first: the status was admitted as a new generation at t1+3ms and the close, now "predating
admission", was ignored - a phantom live session stayed in Top Shelf until its 24h/48h TTL. Each ordering
test runs BOTH processing orders (on two otherwise identical panes) and asserts the same outcome.
"""

import time
import unittest

from herdr_bartender import process
from herdr_bartender.handlers import handle_agent_status_changed, handle_pane_closed, handle_tab_closed
from herdr_bartender.markers import get_hex_pane_id, touch_pane_marker
from herdr_bartender.spool import enqueue_spool, replay_spool_dir
from tests.support import SandboxTestCase

TRAILING_NS = 3_000_000          # the trailing status arrives 3ms after the close
CLOSE_FIRST, STATUS_FIRST = "close-first", "status-first"
LIVE_STATES = ("Working", "Waiting", "Idle", "Done", "Blocked")


def _status(pane, status, **extra):
    return {"agent_status": status, "pane_id": pane, "workspace_id": "w1", "tab_id": f"{pane}-tab", **extra}


class CloseOrderingCase(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.t1 = time.time_ns()

    def _data(self):
        with self.cache_mgr as data:
            return data

    def _send_status(self, data, offset_ns=TRAILING_NS):
        handle_agent_status_changed(data, {}, bridge_url=self.mock_url, arrival_ns=self.t1 + offset_ns)

    def _pane_close(self, pane):
        handle_pane_closed({"pane_id": pane, "workspace_id": "w1"}, {}, bridge_url=self.mock_url, arrival_ns=self.t1)

    def _tab_close(self, pane):
        handle_tab_closed({"tab_id": f"{pane}-tab", "workspace_id": "w1"}, {}, bridge_url=self.mock_url,
                          arrival_ns=self.t1)

    def _close_outcome(self, pane):
        data = self._data()
        record = data["sessions"].get(self.sid(pane))
        live = bool(record) and record.get("desired_state") != "Ended"
        return {"live": live, "state": record["desired_state"] if live else None,
                "tombstoned": pane in data["tombstones"],
                "bartender_live": self.sid(pane) in self.bridge.sessions}

    def _both_orders(self, name, close, statuses, prepare=None):
        """``statuses``: (status, extra, offset_ns) processed before (status-first) or after (close-first) ``close``."""
        outcomes = {}
        for order in (CLOSE_FIRST, STATUS_FIRST):
            pane = f"w1:p{name}{'A' if order == CLOSE_FIRST else 'B'}"
            if prepare:
                prepare(pane)
            if order == CLOSE_FIRST:
                close(pane)
            for status, extra, offset in statuses:
                self._send_status(_status(pane, status, **extra), offset)
            if order == STATUS_FIRST:
                close(pane)
            outcomes[order] = self._close_outcome(pane)
        self.assertEqual(outcomes[CLOSE_FIRST], outcomes[STATUS_FIRST], outcomes)
        return outcomes[CLOSE_FIRST]


DEAD = {"live": False, "state": None, "tombstoned": True, "bartender_live": False}


class PaneCloseOrderingTests(CloseOrderingCase):
    def test_trailing_non_working_status_on_an_unseen_pane_never_outlives_the_close(self):
        """The finding's repro: no cached session, a trailing idle/done/blocked carrying an agent at t1+3ms."""
        for status in ("idle", "done", "blocked"):
            with self.subTest(status=status):
                outcome = self._both_orders(f"Unseen{status}", self._pane_close,
                                            [(status, {"agent": "claude"}, TRAILING_NS)])
                self.assertEqual(outcome, DEAD)

    def test_status_first_phantom_is_ended_at_bartender_and_tombstoned(self):
        """Status first: the phantom Idle already reached Bartender; the close ends it there and records the
        tombstone, which then rejects further trailing events (before the fix none was recorded)."""
        pane, sid = "w1:pPhantom", self.sid("w1:pPhantom")
        self._send_status(_status(pane, "idle", agent="claude"))
        self.assertEqual([e["state"] for e in self.bridge.events_for(sid)], ["Idle"])
        self._pane_close(pane)
        data = self._data()
        self.assertNotIn(sid, data["sessions"], "the close's confirmed Ended evicted the phantom")
        self.assertEqual(data["tombstones"][pane]["closed_at_ns"], self.t1)
        self.assertEqual([e["state"] for e in self.bridge.events_for(sid)], ["Idle", "Ended"])
        self.assertNotIn(sid, self.bridge.sessions)
        self._send_status(_status(pane, "done", agent="claude"), 2 * TRAILING_NS)
        self.assertNotIn(sid, self._data()["sessions"], "the tombstone now rejects trailing events")

    def test_positive_working_admission_after_the_close_survives_in_both_orders(self):
        """Control: a working event with event.data.agent (no source ts, Herdr alive) pops the tombstone when the
        close is first, so a close processed after it leaves it alone (Plan L400: a real admission)."""
        outcome = self._both_orders("Positive", self._pane_close, [("working", {"agent": "claude"}, TRAILING_NS)])
        self.assertEqual(outcome, {"live": True, "state": "Working", "tombstoned": False, "bartender_live": True})

    def test_pre_close_source_timestamp_is_ended_in_both_orders(self):
        """A trailing working+agent emitted before the close (source ts below the close's arrival) is rejected when
        the close is first, so it does not survive a close processed after it."""
        stale_src = self.t1 / 1e9 - 1.0
        outcome = self._both_orders("StaleSrc", self._pane_close,
                                    [("working", {"agent": "claude", "timestamp": stale_src}, TRAILING_NS)])
        self.assertEqual(outcome, DEAD)

    def test_later_positive_event_of_the_same_generation_keeps_it(self):
        """A trailing idle (rejected when the close is first) then a positive working (which pops the tombstone):
        live Working in both orders."""
        outcome = self._both_orders("LaterPositive", self._pane_close,
                                    [("idle", {"agent": "claude"}, TRAILING_NS),
                                     ("working", {"agent": "claude"}, 2 * TRAILING_NS)])
        self.assertEqual(outcome, {"live": True, "state": "Working", "tombstoned": False, "bartender_live": True})

    def test_spooled_close_replayed_after_the_trailing_status_ends_it(self):
        """Contention variant: the close lost the lock wait and was spooled, the trailing status took the lock
        first, and the reconciler's replay of the close (arrival t1) still ends the phantom."""
        pane = "w1:pSpooled"
        self._send_status(_status(pane, "idle", agent="claude"))
        self.assertEqual(self._data()["sessions"][self.sid(pane)]["desired_state"], "Idle")
        enqueue_spool("pane.closed", {"pane_id": pane, "workspace_id": "w1"}, {}, arrival_ns=self.t1)
        replay_spool_dir(self.state_dir)
        data = self._data()
        self.assertEqual(data["sessions"][self.sid(pane)]["desired_state"], "Ended")
        self.assertEqual(data["tombstones"][pane]["closed_at_ns"], self.t1)

    def test_herdr_down_positive_admission_does_not_outlive_the_close(self):
        """Positive admission also needs a live Herdr (Plan §4.3 L420), judged when the close is processed."""
        pane = "w1:pHerdrDown"
        self._send_status(_status(pane, "working", agent="claude"))
        self.clear_fake_processes()
        self.add_fake_process("Bartender 6", pid=424200)
        process.reset_caches()
        process.is_herdr_alive()   # the memo the close consults under its lock now says "dead"
        self._pane_close(pane)
        data = self._data()
        self.assertNotIn(self.sid(pane), data["sessions"])
        self.assertIn(pane, data["tombstones"])


class TabCloseOrderingTests(CloseOrderingCase):
    def _cached_ended_session(self, pane):
        """The pane's previous turn ended (agent exit) and its Ended is still cached (bridge failing)."""
        self.bridge.return_code = 200
        self._send_status(_status(pane, "working", agent="claude"), -2_000_000_000)
        self.bridge.return_code = 500
        self._send_status(_status(pane, "idle", agent=""), -1_000_000_000)
        record = self._data()["sessions"][self.sid(pane)]
        self.assertEqual((record["desired_state"], record["tab_id"]), ("Ended", f"{pane}-tab"))

    def test_trailing_status_on_a_cached_ended_session_never_outlives_the_tab_close(self):
        outcome = self._both_orders("TabEnded", self._tab_close, [("done", {"agent": "claude"}, TRAILING_NS)],
                                    prepare=self._cached_ended_session)
        self.assertEqual((outcome["live"], outcome["tombstoned"]), (False, True), outcome)

    def test_herdr_down_positive_admission_does_not_outlive_the_tab_close(self):
        pane = "w1:pTabHerdrDown"
        self._send_status(_status(pane, "working", agent="claude"))
        self.clear_fake_processes()
        self.add_fake_process("Bartender 6", pid=424200)
        process.reset_caches()
        process.is_herdr_alive()
        self._tab_close(pane)
        data = self._data()
        self.assertNotIn(self.sid(pane), data["sessions"])
        self.assertIn(pane, data["tombstones"])


class LegacyRecordTests(SandboxTestCase):
    start_bridge = False   # pure staging, but log_debug resolves the state dir: keep it sandboxed

    def test_record_without_admission_signal_keeps_the_predating_close_rule(self):
        """A session admitted before admission signals were persisted (in-place upgrade) is protected exactly as
        before: a close that arrived before its admission changes nothing."""
        from herdr_bartender.cache_schema import new_cache
        from herdr_bartender.staging import stage_pane_close_result
        data = new_cache("testhost")
        sid, pane = "herdr:testhost:w1:pLegacy", "w1:pLegacy"
        data["sessions"][sid] = {"pane_id": pane, "desired_state": "Idle", "generation": 3, "seq": 1,
                                 "admitted_at_ns": 2_000}
        staged = stage_pane_close_result(data, pane, {"pane_id": pane}, 1_000)
        self.assertEqual((staged.targets, staged.recorded), ((), False))
        self.assertEqual(data["sessions"][sid]["desired_state"], "Idle")
        self.assertNotIn(pane, data["tombstones"])


class PaneCloseMarkerTests(CloseOrderingCase):
    """Plan §1 L59 / L835 (round-4 low finding): ``remove_pane_marker`` runs under the close's lock."""

    def _marker(self, pane, suffix=""):
        return self.state_dir / "panes" / f"{get_hex_pane_id(pane)}{suffix}"

    def _live_pane_with_marker(self, pane):
        self._send_status(_status(pane, "working", agent="claude"), -TRAILING_NS)
        touch_pane_marker(pane)
        self._marker(pane, ".failed").write_text("1")

    def test_pane_close_removes_the_marker_and_failed_flag_before_its_ended_is_sent(self):
        """Step A removes both under the close's lock: they are already gone when the Ended reaches Bartender
        (before, they lingered until the Ended's Step C confirmed it)."""
        pane, seen = "w1:pMarker", []
        self._live_pane_with_marker(pane)
        self.bridge.on_post = lambda payload: seen.append(
            (payload.get("state"), self._marker(pane).exists(), self._marker(pane, ".failed").exists()))
        self._pane_close(pane)
        self.assertEqual(seen, [("Ended", False, False)])

    def test_replayed_pane_close_removes_the_marker(self):
        pane = "w1:pMarkerSpool"
        self._live_pane_with_marker(pane)
        self.bridge.return_code = 500   # nothing delivers the Ended: replay alone must remove the marker
        enqueue_spool("pane.closed", {"pane_id": pane, "workspace_id": "w1"}, {}, arrival_ns=self.t1)
        replay_spool_dir(self.state_dir)
        self.assertEqual(self._data()["sessions"][self.sid(pane)]["desired_state"], "Ended")
        self.assertFalse(self._marker(pane).exists())
        self.assertFalse(self._marker(pane, ".failed").exists())

    def test_stale_close_keeps_the_live_sessions_marker(self):
        """A close predating a real (positive) admission changes nothing, the marker included."""
        pane = "w1:pMarkerStale"
        self._send_status(_status(pane, "working", agent="claude"))
        touch_pane_marker(pane)
        self._pane_close(pane)
        self.assertEqual(self._data()["sessions"][self.sid(pane)]["desired_state"], "Working")
        self.assertTrue(self._marker(pane).exists())


if __name__ == "__main__":
    unittest.main()
