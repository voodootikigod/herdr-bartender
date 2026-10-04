"""Pane markers, state flags and .vendor_active lifecycle."""

import json
import unittest

from herdr_bartender.handlers import handle_agent_status_changed
from herdr_bartender.markers import (
    clear_delivery_down,
    clear_pane_failed,
    is_delivery_down,
    is_disabled,
    remove_pane_marker,
    touch_delivery_down,
    touch_pane_failed,
    touch_pane_marker,
)
from herdr_bartender.sanitize import get_hex_pane_id
from herdr_bartender.vendor import cleanup_vendor_active
from tests.support import SandboxTestCase


class MarkerVendorTests(SandboxTestCase):
    # WEAK: t9-marker-lifecycle
    def test_p09_hex_marker_lifecycle(self):
        """Plan §10.1 #9: injective hex marker holds a unix timestamp and is removed by remove_pane_marker."""
        test_pane = "w1:test/dangerous:pane..id"
        hex_id = get_hex_pane_id(test_pane)
        self.assertTrue("/" not in hex_id and ".." not in hex_id and ":" not in hex_id, f"Unsafe chars in {hex_id}")
        touch_pane_marker(test_pane)
        marker_path = self.state_dir / "panes" / hex_id
        self.assertTrue(marker_path.exists(), f"Expected marker at {marker_path}")
        self.assertGreater(int(marker_path.read_text().strip()), 0, "Marker should store unix timestamp")
        remove_pane_marker(test_pane)
        self.assertFalse(marker_path.exists(), f"Marker should be removed from {marker_path}")

    # WEAK: t14-disabled
    def test_p14_disabled_flag(self):
        """Plan §10.1 #14: the DISABLED tombstone flag is recognized and its removal is too."""
        disabled_path = self.state_dir / "DISABLED"
        disabled_path.touch()
        self.assertIs(is_disabled(), True, "DISABLED flag should be recognized")
        disabled_path.unlink()
        self.assertIs(is_disabled(), False, "DISABLED flag removal should be recognized")

    # WEAK: t17-delivery-down
    def test_p17_delivery_down_and_pane_failed_markers(self):
        """Plan §10.1 #17: DELIVERY_DOWN and <hex>.failed markers are set and cleared."""
        touch_delivery_down()
        self.assertIs(is_delivery_down(), True, "DELIVERY_DOWN marker must be recognized")
        touch_pane_failed("w1:pFail")
        failed_marker = self.state_dir / "panes" / f"{get_hex_pane_id('w1:pFail')}.failed"
        self.assertTrue(failed_marker.exists(), "Pane failed marker must exist")
        clear_delivery_down()
        self.assertIs(is_delivery_down(), False, "DELIVERY_DOWN marker must be cleared")
        clear_pane_failed("w1:pFail")
        self.assertFalse(failed_marker.exists(), "Pane failed marker must be cleared")

    # WEAK: t1-19-57-62-minor
    def test_p19_vendor_fallthrough_tracking_and_cleanup(self):
        """Plan §10.1 #19: remove_pane_marker spares .vendor_active; cleanup_vendor_active unlinks bare and UUID records."""
        vendor_pane = "w1:pVendor"
        hex_v = get_hex_pane_id(vendor_pane)
        panes_dir = self.state_dir / "panes"
        panes_dir.mkdir(parents=True, exist_ok=True)
        p_marker = panes_dir / hex_v
        p_failed = panes_dir / f"{hex_v}.failed"
        vendor_active_file = panes_dir / f"{hex_v}.vendor_active"
        p_marker.touch(exist_ok=True)
        p_failed.touch(exist_ok=True)
        vendor_active_file.touch(exist_ok=True)

        remove_pane_marker(vendor_pane)
        self.assertFalse(p_marker.exists(), "Pane marker must be removed by remove_pane_marker")
        self.assertFalse(p_failed.exists(), "Failed marker must be removed by remove_pane_marker")
        self.assertTrue(vendor_active_file.exists(), "Bare vendor active marker must not be removed by remove_pane_marker")

        cleanup_vendor_active(vendor_pane, bridge_url=self.mock_url)
        self.assertFalse(vendor_active_file.exists(), "Bare vendor active marker must be unlinked by cleanup_vendor_active on confirmed delivery")

        vendor_active_file.write_text(json.dumps({"vendor_session_id": "test_v_uuid_123"}), encoding="utf-8")
        cleanup_vendor_active(vendor_pane, bridge_url=self.mock_url)
        self.assertFalse(vendor_active_file.exists(), "UUID-bearing vendor active marker must be unlinked after dismissal")

    # WEAK: t30-outside-lock
    def test_p30_stranded_vendor_dismissal(self):
        """Plan §10.1 #30: confirmed delivery dismisses a stranded vendor UUID via an Ended outside the lock."""
        stranded_file = self.state_dir / "panes" / f"{get_hex_pane_id('w1:pStranded')}.vendor_active"
        stranded_file.parent.mkdir(parents=True, exist_ok=True)
        stranded_file.write_text(json.dumps({"vendor_session_id": "vendor_stranded_uuid_999"}), encoding="utf-8")
        self.assertTrue(stranded_file.exists())

        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": "w1:pStranded", "workspace_id": "w1", "agent": "claude"},
            {}, bridge_url=self.mock_url,
        )
        self.assertFalse(stranded_file.exists(), ".vendor_active must be unlinked after cleanup")
        dismissed = any(
            h.get("session_id") == "vendor_stranded_uuid_999" and h.get("state") == "Ended"
            for h in self.bridge.history
        )
        self.assertTrue(dismissed, "Ended event for vendor_stranded_uuid_999 must have been sent to bridge")

    # WEAK: t44-cross-ws
    def test_p44_cross_workspace_marker_isolation(self):
        """Plan §10.1 #44: the same raw pane ID in two workspaces yields distinct markers; no raw marker is made."""
        pid_w1 = "w1:pSame"
        touch_pane_marker(pid_w1)
        marker_w1 = self.state_dir / "panes" / get_hex_pane_id(pid_w1)
        marker_w2 = self.state_dir / "panes" / get_hex_pane_id("w2:pSame")
        marker_raw = self.state_dir / "panes" / get_hex_pane_id("pSame")
        self.assertTrue(marker_w1.exists(), "w1 marker must exist")
        self.assertFalse(marker_w2.exists(), "w2 marker must NOT exist")
        self.assertFalse(marker_raw.exists(), "Raw un-scoped marker must NEVER be created")
        remove_pane_marker(pid_w1)
        self.assertFalse(marker_w1.exists(), "w1 marker removed")

    # WEAK: t45-bare-60s
    def test_p45_bare_touch_cleanup(self):
        """Plan §10.1 #45: a bare-touch .vendor_active is unlinked on confirmed delivery and on pane close."""
        bare_pane = "w1:pBareVendor"
        bare_file = self.state_dir / "panes" / f"{get_hex_pane_id(bare_pane)}.vendor_active"
        bare_file.parent.mkdir(parents=True, exist_ok=True)
        bare_file.touch()
        self.assertTrue(bare_file.exists() and bare_file.stat().st_size == 0)

        cleanup_vendor_active(bare_pane, is_pane_closed=False, bridge_url=self.mock_url)
        self.assertFalse(bare_file.exists(), "Bare touch must be unlinked on confirmed delivery")

        bare_file.touch()
        cleanup_vendor_active(bare_pane, is_pane_closed=True, bridge_url=self.mock_url)
        self.assertFalse(bare_file.exists(), "Bare touch must be unlinked on pane close")


if __name__ == "__main__":
    unittest.main()
