"""Pane markers, state flags and .vendor_active lifecycle."""

import json
import os
import time
import unittest

from herdr_bartender.background import run_reconcile_background
from herdr_bartender.handlers import handle_agent_status_changed, handle_pane_closed
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
from herdr_bartender.vendor import (
    VendorFile,
    cleanup_vendor_active,
    parse_vendor_uuid,
    parse_vendor_uuids,
    resolve_vendor_cleanups,
)
from tests.support import SandboxTestCase


class MarkerVendorTests(SandboxTestCase):
    def test_p09_marker_lifecycle_through_delivery_close_and_heartbeat(self):
        """Plan §10.1 #9 (gap t9-marker-lifecycle): no marker after a failed (500) delivery, a marker after the
        confirmed one, its mtime refreshed by the reconciler heartbeat, and removal on pane close."""
        pane = "w1:pMark"
        marker = self.state_dir / "panes" / get_hex_pane_id(pane)
        event = {"agent_status": "working", "pane_id": pane, "workspace_id": "w1", "agent": "claude"}
        self.bridge.return_code = 500
        handle_agent_status_changed(event, {}, bridge_url=self.mock_url)
        self.assertFalse(marker.exists(), "no marker without a confirmed delivery")
        self.bridge.return_code = 200
        handle_agent_status_changed({**event, "agent_status": "blocked"}, {}, bridge_url=self.mock_url)
        self.assertTrue(marker.exists())
        aged = time.time() - 120
        os.utime(marker, (aged, aged))
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        self.assertGreater(marker.stat().st_mtime, aged + 60, "the heartbeat refreshed the marker")
        handle_pane_closed({"pane_id": pane}, {}, bridge_url=self.mock_url)
        self.assertFalse(marker.exists())

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

    def test_p17_delivery_down_and_pane_failed_markers(self):
        """Plan §10.1 #17 helpers (the delivery-driven test is tests/test_response_matrix.DeliveryDownTests)."""
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

        vendor_active_file.write_text(json.dumps({"vendor_session_id": "test_v_uuid_1234567"}), encoding="utf-8")
        cleanup_vendor_active(vendor_pane, bridge_url=self.mock_url)
        self.assertFalse(vendor_active_file.exists(), "UUID-bearing vendor active marker must be unlinked after dismissal")

    def test_p30_stranded_vendor_dismissal(self):
        """Plan §10.1 #30: confirmed delivery dismisses a stranded vendor UUID via an Ended outside the lock
        (lock state at the POST is asserted in tests/test_vendor_dismissal)."""
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

    def test_p44_cross_workspace_isolation(self):
        """Plan §10.1 #44 (gap t44-cross-ws): the same raw pane id in two workspaces gives two sessions and two
        markers; closing w1's pane leaves w2's session, marker and .vendor_active untouched."""
        for ws in ("w1", "w2"):
            handle_agent_status_changed({"agent_status": "working", "pane_id": "pSame", "workspace_id": ws,
                                         "agent": "claude"}, {}, bridge_url=self.mock_url)
        sid1, sid2 = self.sid("w1:pSame"), self.sid("w2:pSame")
        hex1, hex2 = get_hex_pane_id("w1:pSame"), get_hex_pane_id("w2:pSame")
        self.assertNotEqual(sid1, sid2)
        self.assertNotEqual(hex1, hex2)
        panes = self.state_dir / "panes"
        self.assertTrue((panes / hex1).exists() and (panes / hex2).exists())
        self.assertFalse((panes / get_hex_pane_id("pSame")).exists(), "no raw, unscoped marker")
        vendor2 = panes / f"{hex2}.vendor_active"
        vendor2.write_text(json.dumps({"vendor_session_id": "vendor_w2_uuid_000001"}))
        handle_pane_closed({"pane_id": "pSame", "workspace_id": "w1"}, {}, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            self.assertNotIn(sid1, data["sessions"])
            self.assertEqual(data["sessions"][sid2]["desired_state"], "Working")
            self.assertNotIn("w2:pSame", data["tombstones"])
        self.assertFalse((panes / hex1).exists())
        self.assertTrue((panes / hex2).exists())
        self.assertTrue(vendor2.exists())
        self.assertIn(sid2, self.bridge.sessions)

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


class ParseVendorUuidsTests(unittest.TestCase):
    def _file(self, content_dict: dict | None) -> VendorFile:
        from pathlib import Path
        raw = json.dumps(content_dict).encode("utf-8") if content_dict is not None else None
        return VendorFile(path=Path("/tmp/fake.vendor_active"), content=raw, identity=(1, 1))

    def test_single_vendor_session_id_string(self):
        f = self._file({"vendor_session_id": "test-uuid-valid-0001"})
        self.assertEqual(parse_vendor_uuids(f), ("test-uuid-valid-0001",))
        self.assertEqual(parse_vendor_uuid(f), "test-uuid-valid-0001")

    def test_pending_dismissal_space_and_comma_separated(self):
        f = self._file({
            "vendor_session_id": "test-uuid-valid-0001",
            "pending_dismissal_sid": "test-uuid-valid-0002, test-uuid-valid-0003 test-uuid-valid-0004",
        })
        expected = (
            "test-uuid-valid-0001",
            "test-uuid-valid-0002",
            "test-uuid-valid-0003",
            "test-uuid-valid-0004",
        )
        self.assertEqual(parse_vendor_uuids(f), expected)
        self.assertEqual(parse_vendor_uuid(f), "test-uuid-valid-0001")

    def test_list_formats(self):
        f = self._file({
            "vendor_session_id": ["test-uuid-valid-0001", "test-uuid-valid-0002"],
            "pending_dismissal_sid": ["test-uuid-valid-0003", "test-uuid-valid-0004"],
        })
        expected = (
            "test-uuid-valid-0001",
            "test-uuid-valid-0002",
            "test-uuid-valid-0003",
            "test-uuid-valid-0004",
        )
        self.assertEqual(parse_vendor_uuids(f), expected)

    def test_deduplication_preserves_order(self):
        f = self._file({
            "vendor_session_id": "test-uuid-valid-0001",
            "pending_dismissal_sid": "test-uuid-valid-0002 test-uuid-valid-0001 test-uuid-valid-0003",
        })
        self.assertEqual(parse_vendor_uuids(f), ("test-uuid-valid-0001", "test-uuid-valid-0002", "test-uuid-valid-0003"))

    def test_invalid_parts_and_sanitization(self):
        f = self._file({
            "vendor_session_id": "bad!char",
            "pending_dismissal_sid": "short test-uuid-valid-0002 invalid;semicolon test-uuid-valid-0003",
        })
        self.assertEqual(parse_vendor_uuids(f), ("test-uuid-valid-0002", "test-uuid-valid-0003"))
        self.assertEqual(parse_vendor_uuid(f), "test-uuid-valid-0002")

    def test_disallowed_field_names_rejected(self):
        f = self._file({
            "vendor_session_id": "vendor_session_id",
            "pending_dismissal_sid": "pending_dismissal_sid test-uuid-valid-0002 terminal",
        })
        self.assertEqual(parse_vendor_uuids(f), ("test-uuid-valid-0002",))
        self.assertEqual(parse_vendor_uuid(f), "test-uuid-valid-0002")

    def test_empty_and_unreadable_records(self):
        self.assertEqual(parse_vendor_uuids(self._file(None)), ())
        self.assertIsNone(parse_vendor_uuid(self._file(None)))
        self.assertEqual(parse_vendor_uuids(self._file({})), ())
        self.assertIsNone(parse_vendor_uuid(self._file({})))


class MultiUuidReconcilerStagingTests(SandboxTestCase):
    def test_resolve_vendor_cleanups_multi_uuid(self):
        """resolve_vendor_cleanups stages all valid UUIDs from a multi-UUID record."""
        pane = "w1:pMultiStage"
        hex_p = get_hex_pane_id(pane)
        va_file = self.state_dir / "panes" / f"{hex_p}.vendor_active"
        va_file.parent.mkdir(parents=True, exist_ok=True)
        va_file.write_text(json.dumps({
            "vendor_session_id": "test-stage-uuid-0001",
            "pending_dismissal_sid": "test-stage-uuid-0002 test-stage-uuid-0003",
        }))

        with self.cache_mgr as data:
            res = resolve_vendor_cleanups(data, [pane], now=100.0)
            self.assertEqual(
                res.dismissals,
                ("test-stage-uuid-0001", "test-stage-uuid-0002", "test-stage-uuid-0003"),
            )
            for sid in ("test-stage-uuid-0001", "test-stage-uuid-0002", "test-stage-uuid-0003"):
                self.assertIn(sid, data["dismissed_vendor_uuids"])
            res.commit()

        self.assertFalse(va_file.exists())

    def test_stage_stale_multi_uuid(self):
        """_stage_stale stages all UUIDs from an orphaned stale vendor file before retiring it."""
        from herdr_bartender.dismissals import _stage_stale
        from herdr_bartender.vendor import retire_vendor_file
        pane = "w1:pStaleMulti"
        hex_p = get_hex_pane_id(pane)
        va_file = self.state_dir / "panes" / f"{hex_p}.vendor_active"
        va_file.parent.mkdir(parents=True, exist_ok=True)
        va_file.write_text(json.dumps({
            "vendor_session_id": "test-stale-uuid-0001",
            "pending_dismissal_sid": "test-stale-uuid-0002, test-stale-uuid-0003",
        }))

        with self.cache_mgr as data:
            retired = _stage_stale(data, [va_file], now=200.0)
            self.assertEqual(len(retired), 1)
            for sid in ("test-stale-uuid-0001", "test-stale-uuid-0002", "test-stale-uuid-0003"):
                self.assertIn(sid, data["dismissed_vendor_uuids"])
            for rec in retired:
                retire_vendor_file(rec)

        self.assertFalse(va_file.exists())

    def test_multi_pass_reconciler_unlinks_record_and_terminates(self):
        """Processing a vendor active file unlinks the file and bounds dismissals so no unbounded retry loop occurs."""
        pane = "w1:pMultiPass"
        hex_p = get_hex_pane_id(pane)
        va_file = self.state_dir / "panes" / f"{hex_p}.vendor_active"
        va_file.parent.mkdir(parents=True, exist_ok=True)
        va_file.write_text(json.dumps({
            "vendor_session_id": "test-multipass-0001",
            "pending_dismissal_sid": "test-multipass-0002",
        }))

        with self.cache_mgr as data:
            res = resolve_vendor_cleanups(data, [pane], now=100.0)
            self.assertEqual(len(res.unlink), 1, "file must be unlinked after staging")
            res.commit()

        self.assertFalse(va_file.exists(), ".vendor_active must be unlinked to prevent re-reading on subsequent passes")

        # Second pass over same pane sees no file and does not re-stage
        with self.cache_mgr as data:
            res2 = resolve_vendor_cleanups(data, [pane], now=101.0)
            self.assertEqual(len(res2.unlink), 0)
            self.assertEqual(len(res2.dismissals), 0)

    def test_stage_dismissal_hex_preserves_attempts_and_upgrades_closed(self):
        """Re-staging an existing UUID preserves timestamp and attempts to bound retries, and upgrades pane_closed via OR."""
        from herdr_bartender.vendor import stage_dismissal_hex
        data = {
            "dismissed_vendor_uuids": {
                "test-uuid-bound": {
                    "timestamp": 10.0,
                    "pane_hex": "aa",
                    "attempts": 3,
                    "last_attempt": 12.0,
                    "pane_closed": False,
                }
            }
        }
        # Re-stage with pane_closed=True at now=15.0 (< 10s age)
        entry = stage_dismissal_hex(data, "test-uuid-bound", "aa", now=15.0, pane_closed=True)
        self.assertEqual(entry["timestamp"], 10.0, "timestamp must NOT be reset to bound retries")
        self.assertEqual(entry["attempts"], 3, "attempts must NOT be reset to bound retries")
        self.assertEqual(entry["last_attempt"], 12.0)
        self.assertTrue(entry["pane_closed"], "pane_closed must be upgraded to True via OR")

        # Re-stage again with pane_closed=False at now=18.0: pane_closed must remain True
        entry2 = stage_dismissal_hex(data, "test-uuid-bound", "aa", now=18.0, pane_closed=False)
        self.assertEqual(entry2["timestamp"], 10.0)
        self.assertEqual(entry2["attempts"], 3)
        self.assertTrue(entry2["pane_closed"])

    def test_stage_dismissal_hex_expired_or_exhausted_resets_retry_budget(self):
        """Re-staging an expired (>10s) or exhausted (>=5 attempts) UUID resets to a fresh retry budget."""
        from herdr_bartender.vendor import stage_dismissal_hex
        # Case 1: Expired (>10s old)
        data = {
            "dismissed_vendor_uuids": {
                "test-uuid-expired": {
                    "timestamp": 10.0,
                    "pane_hex": "aa",
                    "attempts": 2,
                    "last_attempt": 12.0,
                    "pane_closed": False,
                }
            }
        }
        entry = stage_dismissal_hex(data, "test-uuid-expired", "bb", now=25.0, pane_closed=True)
        self.assertEqual(entry["timestamp"], 25.0, "expired entry must reset timestamp to now")
        self.assertEqual(entry["attempts"], 0, "expired entry must reset attempts to 0")
        self.assertEqual(entry["pane_hex"], "bb")
        self.assertTrue(entry["pane_closed"])

        # Case 2: Exhausted (attempts >= 5) even if within 10s
        data2 = {
            "dismissed_vendor_uuids": {
                "test-uuid-exhausted": {
                    "timestamp": 10.0,
                    "pane_hex": "aa",
                    "attempts": 5,
                    "last_attempt": 14.0,
                    "pane_closed": False,
                }
            }
        }
        entry2 = stage_dismissal_hex(data2, "test-uuid-exhausted", "cc", now=15.0, pane_closed=False)
        self.assertEqual(entry2["timestamp"], 15.0, "exhausted entry must reset timestamp to now")
        self.assertEqual(entry2["attempts"], 0, "exhausted entry must reset attempts to 0")
        self.assertEqual(entry2["pane_hex"], "cc")

    def test_stage_dismissal_hex_malformed_and_future_timestamps_handled_safely(self):
        """stage_dismissal_hex never crashes on string, None, or future timestamps/attempts, resetting budget."""
        from herdr_bartender.vendor import stage_dismissal_hex
        bad_cases = [
            {"timestamp": "bad_string", "attempts": 1},
            {"timestamp": None, "attempts": 1},
            {"timestamp": True, "attempts": 1},
            {"timestamp": 10.0, "attempts": "five"},
            {"timestamp": 10.0, "attempts": None},
            {"timestamp": 50.0, "attempts": 1},  # future timestamp: now=20.0, now - ts = -30.0 < 0
        ]
        for i, bad in enumerate(bad_cases):
            with self.subTest(case=i):
                data = {"dismissed_vendor_uuids": {"test-uuid-bad": {**bad, "pane_hex": "aa", "pane_closed": False}}}
                entry = stage_dismissal_hex(data, "test-uuid-bad", "aa", now=20.0, pane_closed=True)
                self.assertEqual(entry["timestamp"], 20.0, "malformed or future entry must reset timestamp to now")
                self.assertEqual(entry["attempts"], 0, "malformed or future entry must reset attempts to 0")
                self.assertTrue(entry["pane_closed"])


if __name__ == "__main__":
    unittest.main()


