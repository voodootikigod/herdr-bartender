"""Plan §3.3 response matrix through real delivery (Plan §10.1 #8, #17).

Every row (200 ok:true / ok:false, 3xx, 4xx, 5xx, network) for a non-Ended status send and for an Ended
(pane.closed): delivery_status, the §3.3 delivery_error code, rejected_seq, .failed / marker, minimal retry,
orphan protection, consecutive_failures / DELIVERY_DOWN, and reconciler hand-off.
Gaps: t8-response-matrix, error-classification, t17-delivery-down, close-handlers-consecutive,
pane-closed-retryable.
"""

import json
import unittest

from herdr_bartender.handlers import handle_agent_status_changed, handle_pane_closed, handle_tab_closed
from herdr_bartender.paths import get_orphan_path
from herdr_bartender.sanitize import get_hex_pane_id
from tests.support import SandboxTestCase, run_guard, write_guard_script

NETWORK_DELAY = 0.5   # longer than the 0.2s socket timeout

NON_RETRYABLE = {
    "200 ok:false": (200, b'{"ok":false}', "bridge_rejected"),
    "301": (301, b"", "unexpected_redirect"),
    "302": (302, b"", "unexpected_redirect"),
    "400": (400, b'{"error":"invalid JSON"}', "4xx_client_error"),
    "404": (404, b"", "4xx_client_error"),
}
RETRYABLE = {
    "500": (500, b"", "5xx_server_error", 0.0),
    "network": (200, b'{"ok":true}', "network_timeout", NETWORK_DELAY),
}


def _working(pane, status="working"):
    return {"agent_status": status, "pane_id": pane, "workspace_id": "w1", "agent": "claude", "tab_id": "w1:t1"}


class MatrixCase(SandboxTestCase):
    def _admit(self, pane):
        handle_agent_status_changed(_working(pane), {}, bridge_url=self.mock_url)
        self.assertTrue(self._marker(pane).exists(), "confirmed delivery touches the marker")

    def _marker(self, pane):
        return self.state_dir / "panes" / get_hex_pane_id(pane)

    def _failed(self, pane):
        return self.state_dir / "panes" / f"{get_hex_pane_id(pane)}.failed"

    def _session(self, pane):
        with self.cache_mgr as data:
            return data["sessions"].get(self.sid(pane))

    def _posts(self, pane):
        return [b for b in self.bridge.arrivals if isinstance(b, dict) and b.get("session_id") == self.sid(pane)]

    def _send(self, kind, pane):
        if kind == "Ended":
            handle_pane_closed({"pane_id": pane}, {}, bridge_url=self.mock_url)
        else:
            handle_agent_status_changed(_working(pane, "blocked"), {}, bridge_url=self.mock_url)


class ResponseMatrixTests(MatrixCase):
    def test_p08_success_rows(self):
        for kind in ("Waiting", "Ended"):
            with self.subTest(kind=kind):
                pane = f"w1:pOk{kind}"
                self._admit(pane)
                self._send(kind, pane)
                session = self._session(pane)
                if kind == "Ended":
                    self.assertIsNone(session, "confirmed Ended evicts")
                    self.assertFalse(self._marker(pane).exists())
                else:
                    self.assertEqual((session["delivery_status"], session["delivered_state"], session["delivery_error"]),
                                     ("delivered", "Waiting", None))
                    self.assertTrue(self._marker(pane).exists())

    def test_p08_non_retryable_rows(self):
        for kind in ("Waiting", "Ended"):
            for row, (status, body, error) in NON_RETRYABLE.items():
                with self.subTest(kind=kind, row=row):
                    pane = f"w1:pNr{kind}{row.split()[0]}{len(body)}"
                    self._admit(pane)
                    for _ in range(2):   # the primary and, for Ended, the minimal retry
                        self.bridge.enqueue(status, body)
                    self._send(kind, pane)
                    s = self._session(pane)
                    self.assertEqual((s["delivery_status"], s["delivery_error"], s["rejected_seq"]),
                                     ("non_retryable_failed", error, s["seq"]))
                    self.assertTrue(self._failed(pane).exists())
                    self.assertFalse(self._marker(pane).exists(), "vendor hooks fall through at once")
                    posts = [p["state"] for p in self._posts(pane)[1:]]
                    self.assertEqual(posts, ["Ended", "Ended"] if kind == "Ended" else ["Waiting"])
                    if kind == "Ended":
                        self.assertIs(s["orphaned_ended"], True, "Zero-Data-Loss: retained and mirrored")
                        self.assertIn(self.sid(pane), json.loads(get_orphan_path().read_text())["sessions"])
                    self.bridge._scripted.clear()

    def test_p08_retryable_rows(self):
        for kind in ("Waiting", "Ended"):
            for row, (status, body, error, delay) in RETRYABLE.items():
                with self.subTest(kind=kind, row=row):
                    pane = f"w1:pRt{kind}{row}"
                    self._admit(pane)
                    self.spawner.reset()
                    self.bridge.enqueue(status, body, delay=delay)
                    self._send(kind, pane)
                    s = self._session(pane)
                    self.assertEqual((s["delivery_status"], s["delivery_error"], s["delivery_attempts"]),
                                     ("in_flight", error, 1))
                    self.assertLess(s.get("delivered_seq", 0), s["seq"])
                    self.assertTrue(self._failed(pane).exists())
                    self.assertFalse(self._marker(pane).exists())
                    self.assertEqual(len(self._posts(pane)), 2, "no inline retry of a retryable failure")
                    self.assertEqual(len(self.spawner.calls), 1, "handed to the reconciler")
                    with self.cache_mgr as data:
                        data["consecutive_failures"] = 0
                        self.cache_mgr.save(data)

    def test_p08_rejection_lets_the_vendor_hook_fall_through(self):
        """Plan §3.3: after a rejection the guard no longer suppresses the vendor hook for that pane."""
        pane = "w1:pGuard"
        self._admit(pane)
        self.bridge.enqueue(400)
        self._send("Waiting", pane)
        script = write_guard_script(self.state_dir / "guard.sh", 'echo "PASSTHROUGH"')
        res = run_guard(script, "Working", env_extra={"HERDR_PANE_ID": pane})
        self.assertIn("PASSTHROUGH", res.stdout)
        self.assertTrue((self.state_dir / "panes" / f"{get_hex_pane_id(pane)}.vendor_active").exists())


class DeliveryDownTests(MatrixCase):
    def test_p17_three_failures_across_events_then_recovery(self):
        """Plan §10.1 #17 through real delivery: .failed after the first 500, DELIVERY_DOWN after the third, and a
        later 200 clears both."""
        panes = ["w1:pDown1", "w1:pDown2", "w1:pDown3"]
        down = self.state_dir / "DELIVERY_DOWN"
        self.bridge.return_code = 500
        for count, pane in enumerate(panes, start=1):
            handle_agent_status_changed(_working(pane), {}, bridge_url=self.mock_url)
            self.assertTrue(self._failed(pane).exists())
            with self.cache_mgr as data:
                self.assertEqual(data["consecutive_failures"], count)
            self.assertEqual(down.exists(), count >= 3)
        self.bridge.return_code = 200
        handle_agent_status_changed(_working(panes[0], "blocked"), {}, bridge_url=self.mock_url)
        self.assertFalse(down.exists())
        self.assertFalse(self._failed(panes[0]).exists())
        self.assertTrue(self._marker(panes[0]).exists())
        with self.cache_mgr as data:
            self.assertEqual(data["consecutive_failures"], 0)

    def test_close_handlers_count_consecutive_failures(self):
        """Gaps close-handlers-consecutive / pane-closed-retryable: 5xx on pane.closed and tab.closed count toward
        DELIVERY_DOWN and hand the Ended to the reconciler; a 200 clears the flag."""
        for pane in ("w1:pC1", "w1:pC2", "w1:pC3"):
            handle_agent_status_changed({**_working(pane), "tab_id": f"w1:t{pane[-1]}"}, {}, bridge_url=self.mock_url)
        self.spawner.reset()
        self.bridge.return_code = 503
        handle_pane_closed({"pane_id": "w1:pC1"}, {}, bridge_url=self.mock_url)
        handle_pane_closed({"pane_id": "w1:pC2"}, {}, bridge_url=self.mock_url)
        handle_tab_closed({"tab_id": "w1:t3"}, {}, bridge_url=self.mock_url)
        self.assertTrue((self.state_dir / "DELIVERY_DOWN").exists())
        self.assertEqual(len(self.spawner.calls), 3)
        self.assertTrue((self.state_dir / "reconciler.pending").exists())
        self.bridge.return_code = 200
        handle_pane_closed({"pane_id": "w1:pC1"}, {}, bridge_url=self.mock_url)
        self.assertFalse((self.state_dir / "DELIVERY_DOWN").exists())


if __name__ == "__main__":
    unittest.main()
