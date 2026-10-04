"""Bridge contract: health check and bounded request timeouts."""

import time
import unittest

from herdr_bartender.bridge import check_bridge_health, post_bartender_event
from tests.support import SandboxTestCase


class BridgeContractTests(SandboxTestCase):
    # WEAK: t1-19-57-62-minor
    def test_p01_mock_bridge_health(self):
        """Plan §10.1 #1: /health returns {"ok": true, "port": ...} from the mock bridge."""
        health = check_bridge_health(bridge_url=self.mock_url)
        self.assertTrue(health and health["ok"] is True, "Health check failed")

    # WEAK: t13-process-deadline
    def test_p13_bounded_request_timeout(self):
        """Plan §10.1 #13: a delayed bridge cannot stall a POST past its bounded timeout."""
        self.bridge.delay = 3.0
        t0 = time.monotonic()
        post_bartender_event({"state": "Working", "agent": "Test", "session_id": "timeout-test"},
                             timeout=0.5, bridge_url=self.mock_url)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0, f"Expected bounded exit, took {elapsed}s")


if __name__ == "__main__":
    unittest.main()
