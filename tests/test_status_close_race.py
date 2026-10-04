"""A pane.closed from another process lands while a status send is in flight (Plan §4.3 Step C, tombstoned branch).

The close stages the Ended (seq + 1) and records the tombstone, but it cannot claim the session: the status
sender's lease is alive, so it hands off to the reconciler. That reconciler skips the session for the same
reason and exits. The status Step C, seeing the tombstone, must therefore release its own lease and flag
the reconciler whenever the Ended is still undelivered, whatever the send's outcome; a send that may
have landed is also compensated (shared ``delivery_state.apply_delivery_result`` semantics).
"""

import json
import os
import unittest
from unittest import mock

from herdr_bartender.bridge import DeliveryResult
from herdr_bartender.handlers import handle_agent_status_changed
from herdr_bartender.sender import step_b
from tests.support import SandboxTestCase

PANE = "w1:pRace"
WORKING = {"agent_status": "working", "pane_id": PANE, "workspace_id": "w1", "agent": "claude", "tab_id": "w1:t1"}
IDLE = {**WORKING, "agent_status": "idle"}
OUTCOMES = {
    "success": DeliveryResult("success", None, 200),
    "retryable": DeliveryResult("retryable", "network_timeout", None),
    "non_retryable": DeliveryResult("non_retryable", "4xx_client_error", 400),
}


class StatusSendRacingCloseTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.pending = self.state_dir / "reconciler.pending"
        self.session_id = self.sid(PANE)
        handle_agent_status_changed(WORKING, {}, bridge_url=self.mock_url)  # seq 1 delivered

    def _close_from_another_process(self) -> None:
        envelope = {"event": "pane.closed", "data": {"pane_id": PANE}, "context": {}}
        proc = self.run_cli("pane.closed", input=json.dumps(envelope))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with self.cache_mgr as data:
            record = data["sessions"][self.session_id]
            self.assertEqual((record["desired_state"], record["seq"]), ("Ended", 3), "the close staged the Ended")
            self.assertIn(PANE, data["tombstones"])
            self.assertEqual(record["sending_pid"], os.getpid(), "the close deferred to our live lease")
        # The reconciler that close flagged skips the session (our lease is live) and exits: flag consumed.
        self.pending.unlink()

    def _send_idle_racing_close(self, outcome: str):
        def send(payload, bridge_url=None, timeout=0.2):
            self.assertEqual((payload["state"], payload["seq"]), ("Idle", 2))
            self._close_from_another_process()
            return OUTCOMES[outcome]

        with mock.patch.object(step_b, "send_event", side_effect=send) as sent:
            handle_agent_status_changed(IDLE, {}, bridge_url=self.mock_url)
        self.assertEqual(sent.call_count, 1)
        return self.spawner

    def _assert_released_and_handed_off(self, spawn) -> None:
        with self.cache_mgr as data:
            record = data["sessions"][self.session_id]
        self.assertEqual((record["lease_token"], record["sending_pid"], record["lease_deadline"]), (None, None, None),
                         "the status sender's own lease must be released")
        self.assertEqual((record["desired_state"], record["seq"]), ("Ended", 3))
        self.assertLess(record.get("delivered_seq", 0), record["seq"], "the Ended is still owed")
        self.assertTrue(self.pending.exists(), "the undelivered Ended must be flagged for the reconciler")
        self.assertTrue(spawn.calls, "the reconciler is ensured")

    def _ended_posted(self) -> list:
        return [e for e in self.bridge.events_for(self.session_id) if e.get("state") == "Ended"]

    def test_rejected_send_releases_lease_and_hands_off_the_ended(self):
        spawn = self._send_idle_racing_close("non_retryable")
        self._assert_released_and_handed_off(spawn)
        self.assertEqual(self._ended_posted(), [], "a rejection proves the Idle did not land: no compensation")

    def test_landed_send_is_compensated_and_releases_lease(self):
        spawn = self._send_idle_racing_close("success")
        self._assert_released_and_handed_off(spawn)
        self.assertEqual(len(self._ended_posted()), 1, "an Idle that landed after the close is compensated")

    def test_retryable_send_is_compensated_and_releases_lease(self):
        spawn = self._send_idle_racing_close("retryable")
        self._assert_released_and_handed_off(spawn)
        self.assertEqual(len(self._ended_posted()), 1, "a retryable send may have landed: it is compensated")


if __name__ == "__main__":
    unittest.main()
