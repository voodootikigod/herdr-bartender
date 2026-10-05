"""Plan §10.1 invariants #18-#34: strengthened coverage where the audit found mutation gaps.

#24 (post-close tombstone, ``arrival_ns <= closed_at_ns``): the boundary itself. An event arriving at
exactly the close's arrival stamp is late; one nanosecond later it is not.

#27 (close vs status race, positive admission): every positive-admission criterion of Plan §4.1 L283-291 /
§4.3 L413-421 on its own (``working`` only, explicit non-empty ``event.data.agent``, live Herdr), each with
a control that admits the same event when the tombstone is absent, so a rejection cannot come from
anywhere else. The multi-process variant runs real ``bin/herdr-bartender`` processes while the close's
Ended is held in flight at the mock bridge (Plan §10.2 multi-process harness).
"""

import json
import time
import unittest
from unittest import mock

from herdr_bartender.handlers import handle_agent_status_changed, handle_pane_closed
from herdr_bartender.sender import SendPolicy
from herdr_bartender.sender import lease as lease_module
from tests.support import SandboxTestCase

PANE = "w1:pTomb"
SLOWER_THAN_THE_SOCKET_CAP = 0.25   # > policy.SOCKET_CAP_SECONDS (0.2s): the race window always opens
HOOK_WAIT_SECONDS = 30.0            # socket timeout / lease length that outlast the cross-process hook
WITHIN_WINDOW_NS = 1_000_000_000    # 1s after the close: well inside the 60s tombstone window


def _status(status="working", pane=PANE, **extra):
    return {"agent_status": status, "pane_id": pane, "workspace_id": "w1", **extra}


def _focused(pane=PANE, agent="claude"):
    """A context that admits a ``working`` event without ``event.data.agent`` (Plan §2.2 focused agent)."""
    return {"focused_pane_id": pane, "focused_pane_agent": agent, "tab_id": "w1:t1"}


def _once(predicate, action):
    fired = []

    def hook(payload):
        if not fired and isinstance(payload, dict) and predicate(payload):
            fired.append(payload)
            action(payload)
    return hook


class TombstoneCase(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.session_id = self.sid(PANE)

    def _cache(self):
        with self.cache_mgr as data:
            return data

    def _close_unseen_pane(self, arr_ns: int) -> None:
        """A pane.closed for a pane with no cached session still records the tombstone (Plan §4.3 Step A)."""
        handle_pane_closed({"pane_id": PANE, "workspace_id": "w1"}, {}, bridge_url=self.mock_url, arrival_ns=arr_ns)
        data = self._cache()
        self.assertEqual(data["tombstones"][PANE]["closed_at_ns"], arr_ns)
        self.assertNotIn(self.session_id, data["sessions"])

    def _assert_rejected(self, message: str) -> None:
        data = self._cache()
        self.assertNotIn(self.session_id, data["sessions"], message)
        self.assertIn(PANE, data["tombstones"], "the rejected event must preserve the tombstone")
        self.assertEqual(self.bridge.events_for(self.session_id), [], "nothing reaches Bartender")

    def _assert_admitted(self, state: str, agent: str) -> None:
        data = self._cache()
        self.assertNotIn(PANE, data["tombstones"], "positive admission pops the tombstone")
        record = data["sessions"].get(self.session_id)
        self.assertIsNotNone(record, "positive admission admits a new session")
        self.assertEqual((record["desired_state"], record["raw_agent"]), (state, agent))
        self.assertEqual([e["state"] for e in self.bridge.events_for(self.session_id)], [state])


class TombstoneArrivalBoundaryTests(TombstoneCase):
    def test_p24_status_arriving_exactly_at_close_is_rejected(self):
        """Plan §10.1 #24: ``arrival_ns == closed_at_ns`` is a late event (the rule is ``<=``, not ``<``): a
        working event with positive agent metadata arriving at the close's own stamp cannot resurrect the pane."""
        t_close = time.time_ns()
        handle_agent_status_changed(_status(agent="claude"), {}, bridge_url=self.mock_url, arrival_ns=t_close - 10_000)
        handle_pane_closed({"pane_id": PANE, "workspace_id": "w1"}, {}, bridge_url=self.mock_url, arrival_ns=t_close)
        data = self._cache()
        self.assertNotIn(self.session_id, data["sessions"], "the confirmed Ended evicted the session")
        self.assertEqual(data["tombstones"][PANE]["closed_at_ns"], t_close)
        before = [e["state"] for e in self.bridge.events_for(self.session_id)]
        self.assertEqual(before, ["Working", "Ended"])

        handle_agent_status_changed(_status(agent="claude"), {}, bridge_url=self.mock_url, arrival_ns=t_close)

        data = self._cache()
        self.assertNotIn(self.session_id, data["sessions"], "an event at the close's arrival stamp must not resurrect")
        self.assertEqual(data["tombstones"][PANE]["closed_at_ns"], t_close, "the tombstone is untouched")
        self.assertEqual([e["state"] for e in self.bridge.events_for(self.session_id)], before, "no Working re-sent")

    def test_p24_status_one_nanosecond_after_close_is_not_late(self):
        """Plan §10.1 #24: the late-arrival cut is exactly ``closed_at_ns``: one nanosecond later the same positive
        working event passes the arrival check (and, meeting every positive signal, pops the tombstone)."""
        t_close = time.time_ns()
        self._close_unseen_pane(t_close)
        handle_agent_status_changed(_status(agent="claude"), {}, bridge_url=self.mock_url, arrival_ns=t_close + 1)
        self._assert_admitted("Working", "claude")

    def test_p24_unseen_pane_status_at_close_stamp_is_rejected(self):
        """Plan §10.1 #24: the tombstone of a pane closed with no cached session rejects a status arriving at the
        close's exact stamp, so the pane is never admitted."""
        t_close = time.time_ns()
        self._close_unseen_pane(t_close)
        handle_agent_status_changed(_status(agent="claude"), {}, bridge_url=self.mock_url, arrival_ns=t_close)
        self._assert_rejected("an event at the close's arrival stamp must not admit the closed pane")


class PositiveAdmissionTests(TombstoneCase):
    def test_p27_focused_context_without_event_agent_does_not_pop_tombstone(self):
        """Plan §10.1 #27: within 60s of a close, a ``working`` event without ``event.data.agent`` is rejected even
        when the focused context names an agent (``focused_pane_agent``) that would otherwise admit it."""
        t_close = time.time_ns()
        self._close_unseen_pane(t_close)
        handle_agent_status_changed(_status(), _focused(), bridge_url=self.mock_url,
                                    arrival_ns=t_close + WITHIN_WINDOW_NS)
        self._assert_rejected("focused-context admission is not a positive admission signal within 60s")

    def test_p27_focused_context_admits_when_no_tombstone(self):
        """Plan §10.1 #27 (control): the same agent-less focused ``working`` event IS admitted when the pane has no
        tombstone, so the rejection above comes from the tombstone's positive-admission check alone."""
        handle_agent_status_changed(_status(), _focused(), bridge_url=self.mock_url)
        record = self._cache()["sessions"].get(self.session_id)
        self.assertIsNotNone(record)
        self.assertEqual((record["desired_state"], record["raw_agent"]), ("Working", "claude"))

    def test_p27_blank_event_agent_does_not_pop_tombstone(self):
        """Plan §10.1 #27: an explicitly blank ``event.data.agent`` (whitespace) is not positive metadata, even with
        a focused agent in context."""
        t_close = time.time_ns()
        self._close_unseen_pane(t_close)
        handle_agent_status_changed(_status(agent="   "), _focused(), bridge_url=self.mock_url,
                                    arrival_ns=t_close + WITHIN_WINDOW_NS)
        self._assert_rejected("a blank agent is not a positive admission signal")

    def test_p27_idle_with_agent_does_not_pop_tombstone(self):
        """Plan §10.1 #27: only ``working`` pops a recent tombstone; an ``idle`` event carrying a real agent (which
        Case A would otherwise admit as Idle) is rejected and the tombstone survives."""
        t_close = time.time_ns()
        self._close_unseen_pane(t_close)
        handle_agent_status_changed(_status("idle", agent="claude"), {}, bridge_url=self.mock_url,
                                    arrival_ns=t_close + WITHIN_WINDOW_NS)
        self._assert_rejected("a non-working status must not pop the tombstone")

    def test_p27_idle_with_agent_admits_when_no_tombstone(self):
        """Plan §10.1 #27 (control): the same ``idle`` + agent event is admitted (Case A) without a tombstone."""
        handle_agent_status_changed(_status("idle", agent="claude"), {}, bridge_url=self.mock_url)
        record = self._cache()["sessions"].get(self.session_id)
        self.assertIsNotNone(record)
        self.assertEqual(record["desired_state"], "Idle")

    def test_p27_positive_working_event_pops_tombstone(self):
        """Plan §10.1 #27: a post-close ``working`` event with explicit agent metadata, Herdr alive and no source
        timestamp pops the tombstone within the window and admits a new session."""
        t_close = time.time_ns()
        self._close_unseen_pane(t_close)
        handle_agent_status_changed(_status(agent="claude"), {}, bridge_url=self.mock_url,
                                    arrival_ns=t_close + WITHIN_WINDOW_NS)
        self._assert_admitted("Working", "claude")


class PositiveAdmissionHerdrDeadTests(TombstoneCase):
    default_liveness = False  # Bartender only: no live Herdr process in the shim table

    def setUp(self):
        super().setUp()
        self.add_fake_process("Bartender 6", pid=424200)

    def test_p27_positive_event_with_herdr_down_does_not_pop_tombstone(self):
        """Plan §10.1 #27: positive admission also requires a running Herdr; with Herdr down, a working event with
        agent metadata inside the window is rejected and the tombstone preserved."""
        t_close = time.time_ns()
        self._close_unseen_pane(t_close)
        handle_agent_status_changed(_status(agent="claude"), {}, bridge_url=self.mock_url,
                                    arrival_ns=t_close + WITHIN_WINDOW_NS)
        self._assert_rejected("no positive admission without a live Herdr")

    def test_p27_herdr_down_admits_when_no_tombstone(self):
        """Plan §10.1 #27 (control): with Herdr down the same event is admitted when no tombstone exists."""
        handle_agent_status_changed(_status(agent="claude"), {}, bridge_url=self.mock_url)
        record = self._cache()["sessions"].get(self.session_id)
        self.assertIsNotNone(record)
        self.assertEqual(record["desired_state"], "Working")


class MultiProcessCloseVsStatusTests(TombstoneCase):
    def _run_status_elsewhere(self, data: dict, context: dict) -> dict:
        """Run one status event in a real bin/herdr-bartender process; return its exit and the cache it left."""
        envelope = {"event": "pane.agent_status_changed", "data": data, "context": context}
        proc = self.run_cli("pane.agent_status_changed", input=json.dumps(envelope))
        cache = self._cache()
        record = cache["sessions"].get(self.session_id) or {}
        return {"returncode": proc.returncode, "stderr": proc.stderr, "tombstoned": PANE in cache["tombstones"],
                "state": record.get("desired_state"), "generation": record.get("generation"),
                "seq": record.get("seq")}

    def test_p27_status_processes_racing_an_in_flight_close(self):
        """Plan §10.1 #27, multi-process: while this process's pane.closed Ended is held at the mock bridge, other
        real processes deliver status events for the pane. Within 60s of the close, an agent-less focused working
        event and an idle event with an agent are rejected and leave the tombstone; a working event with positive
        agent metadata pops it and admits a new generation that survives the close's Step C.

        Deterministic by construction: the other processes run inside the bridge's ``on_post`` hook while the
        Ended's request is blocked (socket timeout and lease outlast the hook), and the results are asserted
        on the main thread."""
        handle_agent_status_changed(_status(agent="claude", tab_id="w1:t1"), {}, bridge_url=self.mock_url)
        first_generation = self._cache()["sessions"][self.session_id]["generation"]
        steps = {}

        def race_elsewhere(_payload):
            time.sleep(SLOWER_THAN_THE_SOCKET_CAP)
            try:
                steps["focused"] = self._run_status_elsewhere(_status(), _focused())
                steps["idle"] = self._run_status_elsewhere(_status("idle", agent="claude"), {})
                steps["positive"] = self._run_status_elsewhere(_status(agent="codex"), {})
            except Exception as exc:  # noqa: BLE001 - surfaced on the main thread below
                steps["error"] = repr(exc)

        self.bridge.on_post = _once(lambda p: p.get("state") == "Ended", race_elsewhere)
        with mock.patch.object(SendPolicy, "socket_timeout", lambda _policy: HOOK_WAIT_SECONDS), \
                mock.patch.object(lease_module, "LEASE_SECONDS", HOOK_WAIT_SECONDS):
            handle_pane_closed({"pane_id": PANE, "workspace_id": "w1"}, {}, bridge_url=self.mock_url)

        self.assertNotIn("error", steps, steps)
        for name in ("focused", "idle", "positive"):
            self.assertEqual(steps[name]["returncode"], 0, steps[name]["stderr"])
        for name in ("focused", "idle"):
            self.assertTrue(steps[name]["tombstoned"], f"{name}: the tombstone must survive a non-positive event")
            self.assertEqual((steps[name]["state"], steps[name]["generation"], steps[name]["seq"]),
                             ("Ended", first_generation, 2), f"{name}: the closing session is untouched")
        positive = steps["positive"]
        self.assertFalse(positive["tombstoned"], "positive admission pops the tombstone")
        self.assertEqual(positive["state"], "Working")
        self.assertGreater(positive["generation"], first_generation, "a new session generation is admitted")

        data = self._cache()
        self.assertNotIn(PANE, data["tombstones"])
        record = data["sessions"].get(self.session_id)
        self.assertIsNotNone(record, "the close's Step C keeps the newer admission")
        self.assertEqual((record["desired_state"], record["raw_agent"], record["generation"]),
                         ("Working", "codex", positive["generation"]))


if __name__ == "__main__":
    unittest.main()
