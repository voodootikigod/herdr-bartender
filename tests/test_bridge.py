"""Bridge contract: health check, bounded timeouts, response classification and port validation."""

import os
import socket
import time
import unittest

from herdr_bartender import bridge, config, runtime
from herdr_bartender.bridge import (
    DeliveryResult,
    check_bridge_health,
    classify_response,
    deliver_event,
    post_bartender_event,
)
from tests.support import SandboxTestCase

FAKE_BARTENDER_PID = 4_000_001


class BridgeContractTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.add_fake_process("Bartender 6", pid=FAKE_BARTENDER_PID, lstart="Sat Oct  4 07:00:00 2026")

    def test_p01_mock_bridge_health(self):
        """Plan §10.1 #1 (gap t1-19-57-62-minor): /health returns {"ok": true, "port": <bridge port>}."""
        health = check_bridge_health(bridge_url=self.mock_url)
        self.assertTrue(health and health["ok"] is True, "Health check failed")
        self.assertEqual(health["port"], self.bridge.port)

    # Process-level bound: tests/test_watchdog.py ProcessDeadlineTests (gap t13-process-deadline).
    def test_p13_bounded_request_timeout(self):
        """Plan §10.1 #13: a delayed bridge cannot stall a POST past its bounded timeout."""
        self.bridge.delay = 3.0
        t0 = time.monotonic()
        result = deliver_event({"state": "Working", "agent": "Test", "session_id": "timeout-test"},
                               timeout=0.5, bridge_url=self.mock_url)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0, f"Expected bounded exit, took {elapsed}s")
        self.assertEqual((result.outcome, result.error), ("retryable", "network_timeout"))

    def test_structured_results_per_status_row(self):
        """Plan §3.3 response table (gap error-classification): every row yields its delivery_error code."""
        rows = [
            (200, {"ok": True}, ("success", None), (True, False)),
            (200, {"ok": False}, ("non_retryable", "bridge_rejected"), (False, True)),
            (400, None, ("non_retryable", "4xx_client_error"), (False, True)),
            (404, None, ("non_retryable", "4xx_client_error"), (False, True)),
            (308, None, ("non_retryable", "unexpected_redirect"), (False, True)),
            (500, None, ("retryable", "5xx_server_error"), (False, False)),
            (503, None, ("retryable", "5xx_server_error"), (False, False)),
            (200, "not json", ("retryable", "invalid_response"), (False, False)),
        ]
        payload = {"state": "Working", "agent": "A", "session_id": "s-row"}
        for status, body, expected, legacy in rows:
            with self.subTest(status=status, body=body):
                self.bridge.enqueue(status, body)
                result = deliver_event(payload, bridge_url=self.mock_url)
                self.assertEqual((result.outcome, result.error), expected)
                self.assertEqual(result.as_tuple(), legacy)
                self.bridge.enqueue(status, body)
                self.assertEqual(post_bartender_event(payload, bridge_url=self.mock_url), legacy)

    def test_connection_refused_is_retryable_network_error(self):
        """Plan §3.3 Network Fail row (gap error-classification): refused connections are network_timeout."""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead_port = s.getsockname()[1]
        result = deliver_event({"state": "Working", "agent": "A", "session_id": "s"},
                               bridge_url=f"http://127.0.0.1:{dead_port}")
        self.assertEqual((result.outcome, result.error), ("retryable", "network_timeout"))

    def test_minimal_ended_retry_reports_final_classification(self):
        """Plan §3.3 Ended rows L230/L232, R19 (gaps error-classification, minimal-retry-noop): the minimal
        retry carries the literal agent "Herdr" and its result is the one reported."""
        ended = {"state": "Ended", "agent": "claude", "session_id": "s-end", "seq": 4}
        self.bridge.enqueue(400)
        self.bridge.enqueue(302)
        result = deliver_event(ended, bridge_url=self.mock_url)
        self.assertEqual((result.outcome, result.error), ("non_retryable", "unexpected_redirect"))
        self.assertEqual(self.bridge.requests[-1]["body"], {"state": "Ended", "agent": "Herdr", "session_id": "s-end"})
        self.bridge.enqueue(400)
        self.assertEqual(deliver_event(ended, bridge_url=self.mock_url).outcome, "success")
        self.assertEqual(len(self.bridge.requests), 4)

    def test_network_io_under_the_cache_lock_raises_in_every_mode(self):
        """Plan §1 L9 (`assert not IN_CRITICAL_SECTION`): the bridge refuses I/O under the lock with no
        test-only switch, so production enforces exactly what the suite does."""
        os.environ.pop("HERDR_BARTENDER_UNIT_TESTING", None)
        runtime.IN_CRITICAL_SECTION = True
        try:
            with self.assertRaises(bridge.CriticalSectionViolation):
                deliver_event({"state": "Working", "agent": "A", "session_id": "s-locked"}, bridge_url=self.mock_url)
        finally:
            runtime.IN_CRITICAL_SECTION = False
        self.assertEqual(self.bridge.requests, [])


class BudgetModeTests(SandboxTestCase):
    """Plan §6.1: the ``time_remaining() - 0.3`` socket formula binds only the watchdog-bounded path."""

    def setUp(self):
        super().setUp()
        self.add_fake_process("Bartender 6", pid=FAKE_BARTENDER_PID, lstart="Sat Oct  4 07:00:00 2026")

    @staticmethod
    def _long_running(mode=None):
        runtime.PROCESS_DEADLINE_SECONDS = runtime.DEFAULT_DEADLINE_SECONDS
        runtime.START_TIME = time.monotonic() - 3600
        runtime.set_deadline_mode(mode)

    def test_unbounded_process_uses_caller_timeout(self):
        """Plan §6.1 / L619 reconciler exempt from the 1.5s watchdog (gaps error-classification,
        subprocess-no-timeout): an hour into the reconciler, /health keeps its 0.6s and /event its 0.2s."""
        self._long_running()
        self.assertEqual(bridge.socket_timeout(bridge.DEFAULT_HEALTH_TIMEOUT), 0.6)
        self.assertEqual(bridge.socket_timeout(bridge.DEFAULT_EVENT_TIMEOUT), 0.2)
        self.assertTrue(check_bridge_health(bridge_url=self.mock_url)["ok"])

    def test_bounded_path_applies_plan_formula(self):
        """Plan §6.1 L683 (gap error-classification): on the hot path the socket timeout is
        min(timeout, max(0.05, time_remaining() - 0.3))."""
        self._long_running(runtime.DEADLINE_BOUNDED)
        self.assertEqual(bridge.socket_timeout(0.6), bridge.MIN_SOCKET_TIMEOUT)
        runtime.START_TIME = time.monotonic()
        self.assertEqual(bridge.socket_timeout(0.2), 0.2)

    def test_unbounded_process_still_retries_minimal_ended(self):
        """Plan §3.3 L230 / §9.2 step 5 (gap minimal-retry-noop): the reconciler's Ended retry is not
        suppressed by a pinned time_remaining()."""
        self._long_running()
        self.bridge.enqueue(400)
        ended = {"state": "Ended", "agent": "claude", "session_id": "s-late"}
        self.assertTrue(deliver_event(ended, bridge_url=self.mock_url).success)
        self.assertEqual([r["body"]["agent"] for r in self.bridge.requests], ["claude", "Herdr"])

    def test_bounded_path_skips_retry_without_budget(self):
        """Plan §3.3 L479 (gap minimal-retry-noop): on the hot path the retry needs time_remaining() > 0.3s."""
        self._long_running(runtime.DEADLINE_BOUNDED)
        self.bridge.enqueue(400)
        result = deliver_event({"state": "Ended", "agent": "claude", "session_id": "s-hot"}, bridge_url=self.mock_url)
        self.assertEqual((result.outcome, result.error), ("non_retryable", "4xx_client_error"))
        self.assertEqual(len(self.bridge.requests), 1)


class ClassifyResponseTests(unittest.TestCase):
    """classify_response() is pure: (status, body, error) -> DeliveryResult."""

    def test_classification_table(self):
        """Plan §3.3 L229-234 (gap error-classification): the full classification matrix."""
        cases = [
            ((200, b'{"ok":true}', None), ("success", None)),
            ((200, b'{"ok":true,"sessions":1}', None), ("success", None)),
            ((200, b'{"ok":false}', None), ("non_retryable", "bridge_rejected")),
            ((200, b'{"error":"x"}', None), ("non_retryable", "bridge_rejected")),
            ((200, b'[1,2]', None), ("retryable", "invalid_response")),
            ((200, b'', None), ("retryable", "invalid_response")),
            ((200, b'<html>', None), ("retryable", "invalid_response")),
            ((204, b'', None), ("retryable", "invalid_response")),
            ((301, b'', None), ("non_retryable", "unexpected_redirect")),
            ((302, b'', None), ("non_retryable", "unexpected_redirect")),
            ((399, b'', None), ("non_retryable", "unexpected_redirect")),
            ((400, b'{"error":"invalid JSON"}', None), ("non_retryable", "4xx_client_error")),
            ((499, b'', None), ("non_retryable", "4xx_client_error")),
            ((500, b'', None), ("retryable", "5xx_server_error")),
            ((599, b'', None), ("retryable", "5xx_server_error")),
            ((None, None, socket.timeout("timed out")), ("retryable", "network_timeout")),
            ((None, None, ConnectionRefusedError()), ("retryable", "network_timeout")),
            ((None, None, None), ("retryable", "network_timeout")),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                result = classify_response(*args)
                self.assertIsInstance(result, DeliveryResult)
                self.assertEqual((result.outcome, result.error), expected)
                self.assertEqual(result.http_status, args[0])

    def test_result_is_immutable_and_serialisable(self):
        """Gap error-classification: results are frozen and expose the {outcome, error} shape."""
        result = classify_response(302, b"", None)
        self.assertEqual(result.as_dict(), {"outcome": "non_retryable", "error": "unexpected_redirect",
                                            "http_status": 302})
        self.assertTrue(result.non_retryable)
        self.assertFalse(result.success)
        with self.assertRaises(AttributeError):
            result.outcome = "success"


class PortValidationTests(SandboxTestCase):
    start_bridge = False

    def test_port_validation(self):
        """Plan §3.4 Loopback Bridge Port / §8 L1088 (gap port-validation): 1024-65535 integers, else 7823."""
        cases = {
            "80": 7823, "1023": 7823, "70000": 7823, "65536": 7823, "abc": 7823, "7823/x": 7823,
            "": 7823, " 9000": 7823, "-9000": 7823, "+9000": 7823, "9000.0": 7823, "0x1F90": 7823,
            "123456": 7823, "1024": 1024, "65535": 65535, "9000": 9000,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                os.environ["NOTCHBAR_AGENTS_PORT"] = raw
                self.assertEqual(config.get_bridge_port(), expected)
                self.assertEqual(config.get_bridge_url(), f"http://127.0.0.1:{expected}")

    def test_unset_port_defaults_silently_and_invalid_warns(self):
        """Plan §8 L1088 (gap port-validation): invalid values are logged as a warning; unset is silent."""
        os.environ.pop("NOTCHBAR_AGENTS_PORT", None)
        self.assertEqual(config.get_bridge_port(), config.DEFAULT_PORT)
        log = self.state_dir / "plugin.log"
        self.assertFalse(log.exists() and "NOTCHBAR_AGENTS_PORT" in log.read_text())
        os.environ["NOTCHBAR_AGENTS_PORT"] = "80"
        config.get_bridge_port()
        self.assertIn("NOTCHBAR_AGENTS_PORT", log.read_text())

    def test_bridge_uses_validated_port(self):
        """Plan §8 L1087 (gap port-validation): the default bridge URL is the literal loopback with the validated port."""
        os.environ["NOTCHBAR_AGENTS_PORT"] = "70000"
        self.assertEqual(bridge.default_bridge_url(), "http://127.0.0.1:7823")


if __name__ == "__main__":
    unittest.main()
