"""Background reconciler: singleton, TTLs, recovery, re-sync, drains, heartbeat, dismissals."""

import fcntl
import json
import os
import time
import unittest
from unittest import mock

from herdr_bartender import background
from herdr_bartender.background import run_reconcile_background
from herdr_bartender.markers import is_delivery_down, touch_delivery_down, touch_pane_marker
from herdr_bartender.reconciler import reconcile_active_sessions
from herdr_bartender.sanitize import get_hex_pane_id
from tests.support import SandboxTestCase


class ReconcilerTests(SandboxTestCase):
    def _seed(self, sid, record):
        with self.cache_mgr as data:
            data["sessions"][sid] = record
            self.cache_mgr.save(data)

    # WEAK: t11-helper-singleton
    def test_p11_reconciler_singleton_pending_rescan(self):
        """Plan §10.1 #11: a held reconciler.lock makes a second runner touch reconciler.pending; the owner consumes it."""
        lock_file = self.state_dir / "reconciler.lock"
        pending_file = self.state_dir / "reconciler.pending"
        test_fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(test_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
            self.assertTrue(pending_file.exists(), "Reconciler should touch reconciler.pending when lock is held")
            fcntl.flock(test_fd, fcntl.LOCK_UN)
        finally:
            os.close(test_fd)
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        self.assertFalse(pending_file.exists(), "Reconciler should consume and unlink reconciler.pending")

    # WEAK: t15-ttl
    def test_p15_waiting_exempt_from_24h_ttl(self):
        """Plan §10.1 #15: a 25h-old Waiting session survives while a 25h-old Idle session is ended and evicted."""
        self._seed("herdr:macbook:wWait", {
            "desired_state": "Waiting", "seq": 1, "delivered_seq": 1,
            "last_event_at": time.time() - 90000, "pane_id": "w1:pWait", "agent": "Claude (Herdr)",
        })
        self._seed("herdr:macbook:wIdle", {
            "desired_state": "Idle", "seq": 1, "delivered_seq": 1,
            "last_event_at": time.time() - 90000, "pane_id": "w1:pIdle", "agent": "Claude (Herdr)",
        })
        touch_pane_marker("w1:pWait")
        touch_pane_marker("w1:pIdle")
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            self.assertIn("herdr:macbook:wWait", data["sessions"], "Waiting session must remain in cache")
            self.assertEqual(data["sessions"]["herdr:macbook:wWait"]["desired_state"], "Waiting", "Waiting sessions must be exempt from TTL")
            self.assertNotIn("herdr:macbook:wIdle", data["sessions"], "Idle sessions must transition to Ended and be evicted on confirmed delivery")

    # WEAK: t15-ttl
    def test_p15_waiting_expires_after_48h(self):
        """Plan §10.1 #15: a Waiting session older than 48h expires to Ended and is evicted."""
        self._seed("herdr:macbook:wWaitOld", {
            "desired_state": "Waiting", "seq": 1, "delivered_seq": 1,
            "last_event_at": time.time() - 200000, "pane_id": "w1:pWaitOld", "agent": "Claude (Herdr)",
        })
        touch_pane_marker("w1:pWaitOld")
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            self.assertNotIn("herdr:macbook:wWaitOld", data["sessions"], "Waiting sessions older than 48h must expire to Ended")

    def test_p20_quiet_outage_health_recovery(self):
        """Plan §10.1 #20: a healthy bridge clears DELIVERY_DOWN and re-arms/delivers a retryable_exhausted session."""
        touch_delivery_down()
        self._seed("herdr:macbook:wExhausted", {
            "desired_state": "Waiting", "seq": 1, "delivered_seq": 0,
            "delivery_status": "retryable_exhausted", "delivery_attempts": 5,
            "last_event_at": time.time(), "pane_id": "w1:pExhausted", "agent": "Claude (Herdr)",
            "desired_payload": {"state": "Waiting", "agent": "Claude (Herdr)", "session_id": "herdr:macbook:wExhausted", "seq": 1},
        })
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        self.assertFalse(is_delivery_down(), "Reconciler health recovery must clear DELIVERY_DOWN")
        with self.cache_mgr as data:
            s_ex = data["sessions"].get("herdr:macbook:wExhausted")
            self.assertIsNotNone(s_ex)
            self.assertEqual(s_ex.get("delivered_state"), "Waiting", "Exhausted session must be re-armed and delivered upon bridge health recovery")
            self.assertEqual(s_ex.get("delivery_status"), "delivered")

    # WEAK: t21-bartender-resync
    def test_p21_bartender_pid_change_resync(self):
        """Plan §10.1 #21: a Bartender PID change triggers a full re-sync of active sessions."""
        with self.cache_mgr as data:
            data["last_bartender_pid"] = 99999999
            data["sessions"]["herdr:macbook:wSyncTest"] = {
                "desired_state": "Working", "seq": 2, "delivered_seq": 2, "delivery_status": "delivered",
                "pane_id": "w1:pSyncTest", "agent": "Claude (Herdr)",
                "desired_payload": {"state": "Working", "agent": "Claude (Herdr)", "session_id": "herdr:macbook:wSyncTest", "seq": 2},
            }
            self.cache_mgr.save(data)
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            s_sync = data["sessions"].get("herdr:macbook:wSyncTest")
            self.assertEqual(s_sync["delivered_seq"], 2)
            self.assertEqual(s_sync["delivery_status"], "delivered")

    def test_p47_automatic_orphan_replay(self):
        """Plan §10.1 #47: the reconciler loop replays $HOME/.herdr-bartender-orphans.json when /health is ok."""
        auto_orphan_file = self.home / ".herdr-bartender-orphans.json"
        auto_orphan_sid = self.sid("wAuto:pOrphanReplay")
        self.bridge.sessions[auto_orphan_sid] = {"state": "Working"}
        auto_orphan_file.write_text(json.dumps({"version": 1, "sessions": {
            auto_orphan_sid: {"agent": "Herdr", "generation": 1, "pane_id": "wAuto:pOrphanReplay"}}}))

        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        self.assertFalse(auto_orphan_file.exists(), "Reconciler loop must automatically replay and remove orphan file")
        self.assertNotIn(auto_orphan_sid, self.bridge.sessions, "Orphaned session must be Ended in bridge")

    # WEAK: t54-drain-reverify
    def test_p54_persisted_compensation_and_cleanup_drained(self):
        """Plan §10.1 #54: pending_compensations and pending_vendor_cleanups are drained by the reconciler loop."""
        sid_54 = self.sid("w1:pPersistComp")
        with self.cache_mgr as data:
            data.setdefault("pending_compensations", []).append({
                "session_id": sid_54, "agent": "Herdr", "generation": 1,
                "admitted_at_ns": 1000, "timestamp": time.time(),
            })
            data.setdefault("pending_vendor_cleanups", []).append({
                "pane_id": "w1:pPersistClean", "is_pane_closed": True, "timestamp": time.time(),
            })
            self.cache_mgr.save(data)

        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        with self.cache_mgr as data:
            self.assertEqual(len(data.get("pending_compensations", [])), 0, "Reconciler loop must drain pending compensations")
            self.assertEqual(len(data.get("pending_vendor_cleanups", [])), 0, "Reconciler loop must drain pending vendor cleanups")
        self.assertNotIn(sid_54, self.bridge.sessions)

    # WEAK: t1-19-57-62-minor
    def test_p57_heartbeat_refreshes_idle_markers(self):
        """Plan §10.1 #57: the reconciler heartbeat refreshes an Idle session's 90s-old marker while Herdr is alive."""
        pane_57 = "w1:pIdleHeartbeat"
        marker_57 = self.state_dir / "panes" / get_hex_pane_id(pane_57)
        marker_57.parent.mkdir(parents=True, exist_ok=True)
        marker_57.write_text("old")
        old_mtime = time.time() - 90
        os.utime(marker_57, (old_mtime, old_mtime))
        sid_57 = self.sid(pane_57)
        self._seed(sid_57, {
            "session_id": sid_57, "pane_id": pane_57,
            "desired_state": "Idle", "delivered_state": "Idle", "delivery_status": "delivered",
            "seq": 1, "salvaged": False, "last_event_at": time.time() - 90,
        })
        with mock.patch.object(background, "is_herdr_alive", lambda: True):
            run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        new_mtime = marker_57.stat().st_mtime
        self.assertLess(time.time() - new_mtime, 10, "Idle session marker must be refreshed by reconciler heartbeat while Herdr is alive")

    # WEAK: t60-cancel-triggers
    def test_p60_dismissal_cancelled_on_vendor_fallback(self):
        """Plan §10.1 #60: a fresh .vendor_active cancels a queued vendor-UUID dismissal without sending Ended."""
        pane_60 = "w1:pDismissCancel"
        hex_60 = get_hex_pane_id(pane_60)
        v_sid_60 = "vendor_fallback_uuid_12345"
        with self.cache_mgr as data:
            data.setdefault("dismissed_vendor_uuids", {})[v_sid_60] = {"timestamp": time.time(), "pane_hex": hex_60}
            self.cache_mgr.save(data)
        va_60 = self.state_dir / "panes" / f"{hex_60}.vendor_active"
        va_60.parent.mkdir(parents=True, exist_ok=True)
        va_60.write_text(f'{{"vendor_session_id":"{v_sid_60}"}}')
        self.bridge.sessions[v_sid_60] = {"state": "Working"}
        run_reconcile_background(bridge_url=self.mock_url, loop_once=True)
        with self.cache_mgr as data:
            self.assertNotIn(v_sid_60, data.get("dismissed_vendor_uuids", {}), "Queued dismissal must be canceled")
        self.assertIn(v_sid_60, self.bridge.sessions, "Active vendor fallback session must NOT be dismissed")

    def test_p64_reconciler_ended_preserves_step_a_origins(self):
        """Plan §10.1 #64: a reconciler-confirmed container Ended tombstones with the persisted Step A origins."""
        pane_64 = "w1:pOriginTest"
        sid_64 = self.sid(pane_64)
        orig_closed_at_ns = time.time_ns() - 5_000_000_000
        orig_closed_source_ts = 12345.67
        orig_last_source_ts = 12340.0
        self._seed(sid_64, {
            "state": "Working", "desired_state": "Ended", "seq": 2, "close_kind": "container",
            "pane_id": pane_64, "agent": "claude",
            "closed_at_ns": orig_closed_at_ns, "closed_source_ts": orig_closed_source_ts,
            "last_source_timestamp": orig_last_source_ts,
        })
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            self.assertNotIn(sid_64, data.get("sessions", {}), "Ended session must be evicted on reconcile delivery")
            self.assertIn(pane_64, data.get("tombstones", {}), "Tombstone must be created on container Ended reconcile")
            t_entry = data["tombstones"][pane_64]
            self.assertEqual(t_entry["closed_at_ns"], orig_closed_at_ns, "Tombstone closed_at_ns must match Step A origin")
            self.assertEqual(t_entry["closed_source_ts"], orig_closed_source_ts, "Tombstone closed_source_ts must match Step A origin")
            self.assertEqual(t_entry["last_source_timestamp"], orig_last_source_ts, "Tombstone last_source_timestamp must match Step A origin")

    # WEAK: t65-settling
    def test_p65_dismissal_settling_window(self):
        """Plan §10.1 #65: a queued vendor dismissal is retried inside the 10s window and purged after it."""
        v_uuid_65 = "vendor_settle_uuid_99999"
        hex_65 = get_hex_pane_id("w1:pSettle")
        t_start = time.time()
        with self.cache_mgr as data:
            data.setdefault("dismissed_vendor_uuids", {})[v_uuid_65] = {
                "timestamp": t_start - 3.0, "pane_hex": hex_65, "attempts": 1, "last_attempt": t_start - 3.0,
            }
            self.cache_mgr.save(data)

        # First pass: elapsed 3s (< 10s), attempts 1 (< 5), interval 3s (>= 2s) -> post and retain.
        self.bridge.sessions[v_uuid_65] = {"state": "Working"}
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            self.assertIn(v_uuid_65, data.get("dismissed_vendor_uuids", {}), "Entry must be retained across retry in settling window")
            meta_65 = data["dismissed_vendor_uuids"][v_uuid_65]
            self.assertEqual(meta_65.get("attempts"), 2, "Attempts must be incremented")
            meta_65["timestamp"] = time.time() - 11.0
            self.cache_mgr.save(data)

        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            self.assertNotIn(v_uuid_65, data.get("dismissed_vendor_uuids", {}), "Entry must be purged after settling window elapsed")


if __name__ == "__main__":
    unittest.main()
