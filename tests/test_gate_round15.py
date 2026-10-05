"""npx adversarial-review gate round 15: bounded spool growth and bounded .vendor_active reads (R62, R63)."""

import unittest

from herdr_bartender import spool, vendor
from herdr_bartender.spool import SpoolWriteError, enqueue_spool
from herdr_bartender.vendor import read_vendor_file
from tests.support import SandboxTestCase


def close_event(n):
    return {"pane_id": f"w1:p{n}", "workspace_id": "w1"}


class SpoolHardCapTests(SandboxTestCase):
    """R62: close envelopes are never pruned, but the spool refuses new envelopes past a hard ceiling."""

    def test_close_envelopes_stop_at_the_hard_cap(self):
        for n in range(spool.SPOOL_HARD_CAP):
            enqueue_spool("pane.closed", close_event(n), {})
        with self.assertRaises(SpoolWriteError):
            enqueue_spool("pane.closed", close_event(10_000), {})
        files = list((self.state_dir / "spool").glob("*.json"))
        self.assertEqual(len(files), spool.SPOOL_HARD_CAP, "existing close envelopes are kept, none pruned")

    def test_oversized_envelope_is_refused(self):
        huge = {**close_event(1), "title": "x" * (spool.MAX_ENVELOPE_BYTES + 1)}
        with self.assertRaises(SpoolWriteError):
            enqueue_spool("pane.agent_status_changed", huge, {})
        self.assertEqual(list((self.state_dir / "spool").glob("*.json")), [])


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
