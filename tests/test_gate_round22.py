"""npx adversarial-review gate round 22: safe state-file writes and closed-pane vendor cleanup across a Herdr
outage (R70)."""

import json
import os
import unittest

from herdr_bartender import process
from herdr_bartender.cache import write_cache_file
from herdr_bartender.markers import touch_pane_marker
from herdr_bartender.reconciler import reconcile_active_sessions
from herdr_bartender.sanitize import get_hex_pane_id
from tests.support import SandboxTestCase
from tests.support.nonblocking import call_without_blocking
from tests.support.reconciler_fixtures import pane_file, read_cache
from tests.support.sandbox import DEFAULT_BARTENDER_PID

UUID = "vendor-uuid-0000000070"
PANE = "w1:pOutage"


class SafeStateWriteTests(SandboxTestCase):
    def test_fifo_at_the_marker_path_never_blocks(self):
        """R70: a FIFO planted at panes/<hex> cannot stall a writer (that may hold the cache lock)."""
        panes = self.state_dir / "panes"
        panes.mkdir(parents=True, exist_ok=True)
        fifo = panes / get_hex_pane_id(PANE)
        os.mkfifo(fifo)
        call_without_blocking(self, fifo, lambda: touch_pane_marker(PANE))

    def test_symlink_at_the_marker_path_is_never_followed(self):
        panes = self.state_dir / "panes"
        panes.mkdir(parents=True, exist_ok=True)
        victim = self.tmp / "victim.txt"
        victim.write_text("precious")
        (panes / get_hex_pane_id(PANE)).symlink_to(victim)
        touch_pane_marker(PANE)
        self.assertEqual(victim.read_text(), "precious")

    def test_planted_symlink_at_the_cache_tmp_path_is_never_followed(self):
        """R70: the predictable cache temp file is created O_EXCL; a symlink planted there is replaced, not written
        through."""
        victim = self.tmp / "victim.json"
        victim.write_text("precious")
        cache_file = self.state_dir / "active-sessions.json"
        tmp = cache_file.with_name(f"{cache_file.name}.tmp.{os.getpid()}")
        tmp.symlink_to(victim)
        write_cache_file(cache_file, {"version": 4, "sessions": {}})
        self.assertEqual(victim.read_text(), "precious")
        self.assertEqual(json.loads(cache_file.read_text())["sessions"], {})
        self.assertFalse(cache_file.is_symlink())


class ClosedPaneVendorCleanupAcrossOutageTests(SandboxTestCase):
    """R70: a closed pane's owed vendor cleanup survives a Herdr outage and runs once Herdr is back."""

    def setUp(self):
        super().setUp()
        self.clock = self.use_fake_clock()
        self.vendor = pane_file(self.state_dir, PANE, ".vendor_active")
        self.vendor.parent.mkdir(parents=True, exist_ok=True)
        self.vendor.write_text(json.dumps({"vendor_session_id": UUID}))
        with self.cache_mgr as data:
            data["pending_vendor_cleanups"] = [{"pane_id": PANE, "is_pane_closed": True,
                                                "timestamp": self.clock.time()}]
            self.cache_mgr.save(data)

    def posts(self):
        return [r for r in self.bridge.requests if r["method"] == "POST" and (r["body"] or {}).get("session_id") == UUID]

    def reconcile(self):
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)

    def test_owed_cleanup_waits_out_the_outage_then_dismisses(self):
        self.clear_fake_processes()
        self.add_fake_process("Bartender 6", pid=DEFAULT_BARTENDER_PID)
        process.reset_caches()
        self.reconcile()
        self.assertEqual(self.posts(), [], "nothing is dismissed while Herdr is dead")
        self.assertTrue(self.vendor.exists())
        self.assertEqual([e["pane_id"] for e in read_cache(self.cache_mgr)["pending_vendor_cleanups"]], [PANE],
                         "the closed pane's cleanup stays owed")
        self.set_herdr_alive()
        process.reset_caches()
        for _ in range(3):
            self.reconcile()
            self.clock.advance(2.5)
        self.assertTrue(self.posts(), "the vendor entry is dismissed once Herdr is back")
        self.assertEqual(self.posts()[0]["body"]["state"], "Ended")
        self.assertFalse(self.vendor.exists())
        self.assertEqual(read_cache(self.cache_mgr)["pending_vendor_cleanups"], [])


if __name__ == "__main__":
    unittest.main()
