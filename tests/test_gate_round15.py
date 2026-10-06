"""npx adversarial-review gate round 15: bounded spool growth and bounded .vendor_active reads (R62, R63)."""

import json
import unittest
from unittest import mock

from herdr_bartender import envelopes, spool, vendor
from herdr_bartender.spool import SpoolWriteError, enqueue_spool
from herdr_bartender.vendor import read_vendor_file
from tests.support import SandboxTestCase


def close_event(n):
    return {"pane_id": f"w1:p{n}", "workspace_id": "w1"}


class SpoolHardCapTests(SandboxTestCase):
    """R62/R66: close envelopes are never pruned or lost to the status cap; a newer close for the same container
    replaces the spooled one, and distinct pending containers are bounded by CLOSE_HARD_CAP."""

    def spooled(self):
        return sorted((self.state_dir / "spool").glob("*.json"))

    def test_closes_past_the_status_cap_are_all_kept(self):
        for n in range(spool.SPOOL_CAP + 50):
            enqueue_spool("pane.closed", close_event(n), {})
        self.assertEqual(len(self.spooled()), spool.SPOOL_CAP + 50, "no close is dropped or refused")

    def test_newer_close_for_the_same_pane_replaces_the_spooled_one(self):
        first = enqueue_spool("pane.closed", close_event(1), {}, arrival_ns=1_000)
        second = enqueue_spool("pane.closed", close_event(1), {}, arrival_ns=2_000)
        self.assertEqual(self.spooled(), [second])
        self.assertFalse(first.exists())
        self.assertEqual(json.loads(second.read_text())["arrival_ns"], 2_000)

    def test_failed_replacement_keeps_the_spooled_close(self):
        """R67: the older close is removed only after its replacement is durable; a failed write loses nothing."""
        first = enqueue_spool("pane.closed", close_event(1), {}, arrival_ns=1_000)
        with mock.patch.object(spool, "write_bytes_atomic", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(SpoolWriteError):
                enqueue_spool("pane.closed", close_event(1), {}, arrival_ns=2_000)
        self.assertEqual(self.spooled(), [first])

    def test_distinct_closes_stop_at_the_close_ceiling(self):
        with mock.patch.object(spool, "CLOSE_HARD_CAP", 30):
            for n in range(30):
                enqueue_spool("pane.closed", close_event(n), {})
            with self.assertRaises(SpoolWriteError):
                enqueue_spool("pane.closed", close_event(10_000), {})
            enqueue_spool("pane.closed", close_event(5), {})   # a known container is still replaced, never lost
        self.assertEqual(len(self.spooled()), 30)

    def test_status_envelopes_are_still_pruned_to_the_status_cap(self):
        for n in range(spool.SPOOL_CAP + 10):
            enqueue_spool("pane.agent_status_changed",
                          {"pane_id": f"w1:s{n}", "workspace_id": "w1", "agent": "claude", "agent_status": "working"},
                          {})
        self.assertEqual(len(self.spooled()), spool.SPOOL_CAP)

    def test_close_envelope_is_slimmed(self):
        path = enqueue_spool("pane.closed", {**close_event(1), "junk": "x" * 100_000}, {"focused_pane_id": "y" * 9})
        env = json.loads(path.read_text())
        self.assertNotIn("junk", env["event_data"])
        self.assertEqual(env["context"], {})
        self.assertLess(path.stat().st_size, 4096)

    def test_oversized_envelope_is_refused(self):
        huge = {**close_event(1), "agent": "claude", "agent_status": "working",
                "title": "x" * (envelopes.MAX_ENVELOPE_BYTES + 1)}
        with self.assertRaises(SpoolWriteError):
            enqueue_spool("pane.agent_status_changed", huge, {})
        self.assertEqual(self.spooled(), [])


class RelevantCloseNeverLostTests(SandboxTestCase):
    """R83: at the close ceiling, a close that can still end a cached session evicts one that cannot."""

    def spooled(self):
        return sorted((self.state_dir / "spool").glob("*.json"))

    def setUp(self):
        super().setUp()
        import time as _time
        from tests.support.reconciler_fixtures import seed, session
        seed(self.cache_mgr, {self.sid("w1:pLive"): dict(session("w1:pLive", "Working", seq=2, now=_time.time()),
                                                         tab_id="w1:tLive", workspace_id="w1")})

    def test_relevant_close_evicts_an_irrelevant_one(self):
        with mock.patch.object(spool, "CLOSE_HARD_CAP", 3):
            stale = [enqueue_spool("pane.closed", {"pane_id": f"w9:gone{n}", "workspace_id": "w9"}, {})
                     for n in range(3)]
            path = enqueue_spool("pane.closed", {"pane_id": "w1:pLive", "workspace_id": "w1"}, {})
        self.assertIn(path, self.spooled(), "the close that ends a cached session is kept")
        self.assertEqual(len(self.spooled()), 3, "the ceiling still holds")
        self.assertFalse(stale[0].exists(), "the oldest close that targets nothing was evicted")

    def test_relevant_tab_close_evicts_too(self):
        with mock.patch.object(spool, "CLOSE_HARD_CAP", 2):
            for n in range(2):
                enqueue_spool("pane.closed", {"pane_id": f"w9:gone{n}", "workspace_id": "w9"}, {})
            path = enqueue_spool("tab.closed", {"tab_id": "w1:tLive", "workspace_id": "w1"}, {})
        self.assertIn(path, self.spooled())

    def test_irrelevant_close_is_refused_at_the_ceiling(self):
        with mock.patch.object(spool, "CLOSE_HARD_CAP", 2):
            for n in range(2):
                enqueue_spool("pane.closed", {"pane_id": f"w9:gone{n}", "workspace_id": "w9"}, {})
            with self.assertRaises(SpoolWriteError):
                enqueue_spool("pane.closed", {"pane_id": "w8:other", "workspace_id": "w8"}, {})
        self.assertEqual(len(self.spooled()), 2)


class VendorReadBoundTests(SandboxTestCase):
    """R63: a .vendor_active larger than any real record is unreadable (never retired), and never read whole."""

    def test_oversized_vendor_file_is_treated_as_unreadable(self):
        path = vendor.vendor_active_path("w1:pV1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"vendor_session_id":"' + b"a" * (vendor.VENDOR_FILE_MAX_BYTES * 4) + b'"}')
        record = read_vendor_file(path)
        self.assertIsNotNone(record)
        self.assertIsNone(record.content, "oversized content is not loaded")

    def test_normal_record_is_read(self):
        path = vendor.vendor_active_path("w1:pV2")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"vendor_session_id":"a82e9232-0d36-43e6-8b02-1b953babd13e"}')
        self.assertEqual(vendor.parse_vendor_uuid(read_vendor_file(path)), "a82e9232-0d36-43e6-8b02-1b953babd13e")


if __name__ == "__main__":
    unittest.main()
