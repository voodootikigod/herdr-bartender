"""npx adversarial-review gate round 40: .vendor_active retirement (rename/read/unlink) never runs while the cache
lock is held (R88)."""

import json
import time
import unittest
from unittest import mock

from herdr_bartender import runtime, vendor
from herdr_bartender.handlers import handle_agent_status_changed
from herdr_bartender.reconciler import queue_pending_vendor_cleanups
from herdr_bartender.vendor import cleanup_vendor_active
from tests.support import SandboxTestCase
from tests.support.reconciler_fixtures import pane_file

UUID = "vendor-uuid-0000000088"
PANE = "w1:pRetire"


class RetirementOutsideLockTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        real = vendor.retire_vendor_file

        def spy(record):
            self.calls.append(runtime.IN_CRITICAL_SECTION)
            return real(record)

        patcher = mock.patch.object(vendor, "retire_vendor_file", side_effect=spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.va = pane_file(self.state_dir, PANE, ".vendor_active")
        self.va.parent.mkdir(parents=True, exist_ok=True)

    def owe_cleanup(self):
        self.va.write_text(json.dumps({"vendor_session_id": UUID}))
        with self.cache_mgr as data:
            data["pending_vendor_cleanups"] = [{"pane_id": PANE, "is_pane_closed": True, "timestamp": time.time()}]
            self.cache_mgr.save(data)

    def assert_retired_outside_lock(self):
        self.assertTrue(self.calls, "control: a retirement ran")
        self.assertEqual(set(self.calls), {False}, "retirement ran while the cache lock was held")
        self.assertFalse(self.va.exists())

    def test_direct_cleanup(self):
        self.va.write_text(json.dumps({"vendor_session_id": UUID}))
        cleanup_vendor_active(PANE, is_pane_closed=True)
        self.assert_retired_outside_lock()

    def test_reconciler_queue(self):
        self.owe_cleanup()
        queue_pending_vendor_cleanups(self.cache_mgr)
        self.assert_retired_outside_lock()

    def test_step_a_event_path(self):
        self.owe_cleanup()
        handle_agent_status_changed({"pane_id": "w1:pOther", "workspace_id": "w1", "agent": "claude",
                                     "agent_status": "working"}, {}, bridge_url=self.mock_url,
                                    arrival_ns=time.time_ns())
        self.assert_retired_outside_lock()


if __name__ == "__main__":
    unittest.main()
