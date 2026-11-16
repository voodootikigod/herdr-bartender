"""Unit tests for Herdr 0.9.5 ``pane.moved`` and ``pane.exited`` handlers and spool replay."""

from __future__ import annotations

import fcntl
import json
import os
import time
import unittest
from unittest import mock

from herdr_bartender import envelopes, spool
from herdr_bartender.envelopes import PANE_EXITED, PANE_MOVED, is_close_envelope, validate_envelope
from herdr_bartender.handlers import (
    handle_agent_status_changed,
    handle_pane_exited,
    handle_pane_moved,
    handle_tab_closed,
    stage_pane_move,
)
from herdr_bartender.markers import touch_pane_marker
from herdr_bartender.reconciler import reconcile_active_sessions
from herdr_bartender.sanitize import get_hex_pane_id
from herdr_bartender.spool import SPOOL_CAP, enqueue_spool, replay_spool_dir
from herdr_bartender.vendor import vendor_active_path
from tests.support import SandboxTestCase


class PaneMoveAndExitTests(SandboxTestCase):
    def _marker_path(self, canonical_pane: str):
        return self.state_dir / "panes" / get_hex_pane_id(canonical_pane)

    def test_exports_and_constants(self):
        from herdr_bartender import handlers

        self.assertEqual(handlers.PANE_EXITED, "pane.exited")
        self.assertEqual(handlers.PANE_MOVED, "pane.moved")
        self.assertIs(handlers.handle_pane_exited, handle_pane_exited)
        self.assertIs(handlers.handle_pane_moved, handle_pane_moved)
        self.assertIs(handlers.stage_pane_move, stage_pane_move)

    def test_cross_workspace_move_closes_old_pane_and_stages_dest_for_reconciler(self):
        """Moving a live agy session w1:p1 -> w2:p3 ends/tombstones w1:p1 inline (event='pane.moved'),
        removes its marker, and stages w2:p3 Working ('Antigravity (Herdr)') with fresh foreground_cwd,
        title, and tab_id for the reconciler."""
        handle_agent_status_changed(
            {
                "agent_status": "working",
                "pane_id": "w1:p1",
                "workspace_id": "w1",
                "tab_id": "w1:t1",
                "agent": "agy",
                "cwd": "/old/cwd",
                "title": "Old Turn",
            },
            {},
            bridge_url=self.mock_url,
        )
        old_sid = self.sid("w1:p1")
        dest_sid = self.sid("w2:p3")
        self.assertTrue(self._marker_path("w1:p1").exists(), "confirmed admission creates pane marker")

        move_payload = {
            "previous_pane_id": "w1:p1",
            "previous_workspace_id": "w1",
            "previous_tab_id": "w1:t1",
            "pane": {
                "pane_id": "w2:p3",
                "workspace_id": "w2",
                "tab_id": "w2:t3",
                "agent": "agy",
                "display_agent": "Antigravity",
                "agent_status": "working",
                "cwd": "/fallback/cwd",
                "foreground_cwd": "/fresh/fg/cwd",
                "title": "Fresh Move Title",
                "label": "Fallback Label",
                "terminal_title_stripped": "Fallback Term",
                "focused": True,
            },
        }
        posts_before = len(self.bridge.history)
        handle_pane_moved(move_payload, {}, bridge_url=self.mock_url)

        # Inline step delivers at most 1 target (R5): the old pane's Ended with event="pane.moved".
        inline_posts = self.bridge.history[posts_before:]
        self.assertEqual(len(inline_posts), 1)
        self.assertEqual(
            (inline_posts[0]["session_id"], inline_posts[0]["state"], inline_posts[0]["event"]),
            (old_sid, "Ended", "pane.moved"),
        )
        self.assertFalse(self._marker_path("w1:p1").exists(), "old pane marker removed on move")

        with self.cache_mgr as data:
            self.assertNotIn(old_sid, data["sessions"], "delivered Ended evicted from cache")
            self.assertIn("w1:p1", data["tombstones"], "old pane tombstoned")
            dest_rec = data["sessions"].get(dest_sid)
            self.assertIsNotNone(dest_rec, "destination session staged")
            self.assertEqual(dest_rec["desired_state"], "Working")
            self.assertEqual(dest_rec["agent"], "Antigravity (Herdr)")
            self.assertEqual(dest_rec["cwd"], "/fresh/fg/cwd")
            self.assertEqual(dest_rec["title"], "Fresh Move Title")
            self.assertEqual(dest_rec["tab_id"], "w2:t3")
            self.assertEqual(dest_rec["workspace_id"], "w2")
            self.assertEqual(dest_rec["delivery_status"], "in_flight")
            self.assertEqual(dest_rec["desired_payload"]["event"], "pane.moved")

        self.assertTrue((self.state_dir / "reconciler.pending").exists(), "overflow target flags reconciler hand-off")
        self.assertTrue(len(self.spawner.calls) >= 1, "reconciler ensured on overflow")

        # Reconciler delivers the staged destination session and creates its pane marker.
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        dest_events = self.bridge.events_for(dest_sid)
        self.assertEqual(len(dest_events), 1)
        self.assertEqual(
            (
                dest_events[0]["state"],
                dest_events[0]["agent"],
                dest_events[0]["event"],
                dest_events[0]["cwd"],
                dest_events[0]["title"],
            ),
            ("Working", "Antigravity (Herdr)", "pane.moved", "/fresh/fg/cwd", "Fresh Move Title"),
        )
        self.assertTrue(self._marker_path("w2:p3").exists(), "destination marker written after confirmed delivery")

    def test_cross_workspace_move_carries_fallback_agent_and_label_when_agent_omitted(self):
        """When a live session moves and PaneInfo omits 'agent' (or uses label/cwd), fallback metadata carries over."""
        handle_agent_status_changed(
            {
                "agent_status": "working",
                "pane_id": "w1:pCarry",
                "workspace_id": "w1",
                "agent": "agy",
                "cwd": "/carry/cwd",
                "title": "Carry Title",
            },
            {},
            bridge_url=self.mock_url,
        )
        handle_pane_moved(
            {
                "previous_pane_id": "w1:pCarry",
                "previous_workspace_id": "w1",
                "previous_tab_id": "w1:t1",
                "pane": {
                    "pane_id": "w2:pCarryDest",
                    "workspace_id": "w2",
                    "tab_id": "w2:t9",
                    "agent_status": "blocked",
                    "cwd": "/carry/new_cwd",
                    "foreground_cwd": None,
                    "title": "",
                    "label": "Label Title",
                    "terminal_title_stripped": "Stripped Title",
                    "focused": False,
                },
            },
            {},
            bridge_url=self.mock_url,
        )
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        dest_sid = self.sid("w2:pCarryDest")
        with self.cache_mgr as data:
            rec = data["sessions"][dest_sid]
            self.assertEqual(
                (rec["desired_state"], rec["agent"], rec["cwd"], rec["title"], rec["tab_id"]),
                ("Waiting", "Antigravity (Herdr)", "/carry/new_cwd", "Label Title", "w2:t9"),
            )

    def test_same_pane_id_move_updates_metadata_without_closing_or_tombstoning(self):
        """Moving w1:p1 -> w1:p1 within a workspace does NOT close or tombstone w1:p1, keeps its marker,
        and updates tab_id, cwd, and title inline."""
        handle_agent_status_changed(
            {
                "agent_status": "working",
                "pane_id": "w1:p1",
                "workspace_id": "w1",
                "tab_id": "w1:t1",
                "agent": "agy",
                "cwd": "/before/move",
                "title": "Before Move",
            },
            {},
            bridge_url=self.mock_url,
        )
        sid = self.sid("w1:p1")
        self.assertTrue(self._marker_path("w1:p1").exists())

        handle_pane_moved(
            {
                "previous_pane_id": "w1:p1",
                "previous_workspace_id": "w1",
                "previous_tab_id": "w1:t1",
                "pane": {
                    "pane_id": "w1:p1",
                    "workspace_id": "w1",
                    "tab_id": "w1:t2",
                    "agent": "agy",
                    "display_agent": "Antigravity",
                    "agent_status": "working",
                    "cwd": "/before/move",
                    "foreground_cwd": "/after/move",
                    "title": "After Move",
                    "label": None,
                    "terminal_title_stripped": None,
                    "focused": True,
                },
            },
            {},
            bridge_url=self.mock_url,
        )

        self.assertTrue(self._marker_path("w1:p1").exists(), "same-pane move must keep pane marker")
        events = self.bridge.events_for(sid)
        self.assertFalse(any(e["state"] == "Ended" for e in events), "same-pane move must never emit Ended")
        self.assertEqual(
            (events[-1]["state"], events[-1]["event"], events[-1]["cwd"], events[-1]["title"], events[-1]["seq"]),
            ("Working", "pane.moved", "/after/move", "After Move", 2),
        )
        with self.cache_mgr as data:
            self.assertNotIn("w1:p1", data.get("tombstones", {}), "same-pane move must not tombstone w1:p1")
            rec = data["sessions"][sid]
            self.assertEqual((rec["tab_id"], rec["cwd"], rec["title"]), ("w1:t2", "/after/move", "After Move"))

        # Subsequent tab.closed on the new tab w1:t2 matches and closes the session.
        handle_tab_closed({"tab_id": "w1:t2", "workspace_id": "w1"}, {}, bridge_url=self.mock_url)
        with self.cache_mgr as data:
            self.assertNotIn(sid, data.get("sessions", {}))

    def test_shell_pane_move_without_agent_closes_old_without_phantom_session(self):
        """Moving an unassisted shell pane (agent=None, agent_status='idle') tombstones the old pane and removes
        any marker without creating a phantom session on the destination pane."""
        touch_pane_marker("w1:pShell")
        self.assertTrue(self._marker_path("w1:pShell").exists())

        handle_pane_moved(
            {
                "previous_pane_id": "w1:pShell",
                "previous_workspace_id": "w1",
                "previous_tab_id": "w1:t1",
                "pane": {
                    "pane_id": "w2:pShellDest",
                    "workspace_id": "w2",
                    "tab_id": "w2:t1",
                    "agent": None,
                    "display_agent": None,
                    "agent_status": "idle",
                    "cwd": "/Users/dev",
                    "foreground_cwd": "/Users/dev",
                    "title": "zsh",
                    "label": None,
                    "terminal_title_stripped": "zsh",
                    "focused": True,
                },
            },
            {},
            bridge_url=self.mock_url,
        )

        self.assertFalse(self._marker_path("w1:pShell").exists(), "old pane marker removed")
        self.assertFalse(self._marker_path("w2:pShellDest").exists(), "no marker created for shell destination")
        self.assertEqual(self.bridge.history, [], "no bridge POST for unassisted shell move")
        with self.cache_mgr as data:
            self.assertIn("w1:pShell", data.get("tombstones", {}))
            self.assertNotIn(self.sid("w1:pShell"), data.get("sessions", {}))
            self.assertNotIn(self.sid("w2:pShellDest"), data.get("sessions", {}))

    def test_empty_or_invalid_move_payload_is_safe_noop(self):
        handle_pane_moved({}, {"focused_pane_id": "w1:p1", "workspace_id": "w1"}, bridge_url=self.mock_url)
        handle_pane_moved({"previous_pane_id": None, "pane": "not-a-dict"}, {}, bridge_url=self.mock_url)
        self.assertFalse((self.state_dir / "active-sessions.json").exists())
        self.assertEqual(self.bridge.history, [])

    def test_pane_exited_delivers_ended_tombstones_removes_marker_and_cleans_vendor(self):
        """handle_pane_exited stages & delivers Ended with event='pane.exited', tombstones the pane,
        removes its marker, retires .vendor_active, and sends the vendor dismissal."""
        pane = "w1:pExit"
        sid = self.sid(pane)
        vendor_uuid = "a82e9232-0d36-43e6-8b02-1b953babd13e"

        handle_agent_status_changed(
            {"agent_status": "working", "pane_id": pane, "workspace_id": "w1", "agent": "claude"},
            {},
            bridge_url=self.mock_url,
        )
        self.assertTrue(self._marker_path(pane).exists())
        vpath = vendor_active_path(pane)
        vpath.parent.mkdir(parents=True, exist_ok=True)
        vpath.write_text(json.dumps({"vendor_session_id": vendor_uuid}))

        posts_before = len(self.bridge.history)
        handle_pane_exited({"pane_id": pane, "workspace_id": "w1"}, {}, bridge_url=self.mock_url)

        sent = self.bridge.history[posts_before:]
        session_ended = [p for p in sent if p.get("session_id") == sid]
        vendor_ended = [p for p in sent if p.get("session_id") == vendor_uuid]
        self.assertEqual(len(session_ended), 1)
        self.assertEqual(
            (session_ended[0]["state"], session_ended[0]["terminal"], session_ended[0]["event"], session_ended[0]["seq"]),
            ("Ended", "Herdr", "pane.exited", 2),
        )
        self.assertEqual(len(vendor_ended), 1)
        self.assertEqual((vendor_ended[0]["state"], vendor_ended[0]["agent"]), ("Ended", "Herdr"))

        self.assertFalse(self._marker_path(pane).exists(), "pane marker removed on pane.exited")
        self.assertFalse(vpath.exists(), ".vendor_active retired on pane.exited")
        with self.cache_mgr as data:
            self.assertNotIn(sid, data.get("sessions", {}))
            self.assertIn(pane, data.get("tombstones", {}))


class SpoolMoveAndExitReplayTests(SandboxTestCase):
    def _marker_path(self, canonical_pane: str):
        return self.state_dir / "panes" / get_hex_pane_id(canonical_pane)

    def test_contention_defers_pane_moved_and_pane_exited_then_replays_cleanly(self):
        """When active-sessions.lock is held during handle_pane_moved and handle_pane_exited, both defer to spool/
        with valid envelopes (pane.moved preserves nested pane dict; pane.exited is a keyed close) and replay cleanly."""
        handle_agent_status_changed(
            {
                "agent_status": "working",
                "pane_id": "w1:pMoveSrc",
                "workspace_id": "w1",
                "tab_id": "w1:t1",
                "agent": "agy",
                "cwd": "/initial",
                "title": "Initial",
            },
            {},
            bridge_url=self.mock_url,
        )
        handle_agent_status_changed(
            {
                "agent_status": "working",
                "pane_id": "w1:pExitSrc",
                "workspace_id": "w1",
                "tab_id": "w1:t2",
                "agent": "claude",
            },
            {},
            bridge_url=self.mock_url,
        )
        self.assertTrue(self._marker_path("w1:pMoveSrc").exists())
        self.assertTrue(self._marker_path("w1:pExitSrc").exists())

        move_payload = {
            "previous_pane_id": "w1:pMoveSrc",
            "previous_workspace_id": "w1",
            "previous_tab_id": "w1:t1",
            "pane": {
                "pane_id": "w2:pMoveDst",
                "workspace_id": "w2",
                "tab_id": "w2:t5",
                "agent": "agy",
                "display_agent": "Antigravity",
                "agent_status": "working",
                "cwd": "/moved/base",
                "foreground_cwd": "/moved/fg",
                "title": "Moved Under Contention",
                "label": None,
                "terminal_title_stripped": None,
                "focused": True,
            },
        }
        exit_payload = {"pane_id": "w1:pExitSrc", "workspace_id": "w1"}

        posts_before = len(self.bridge.history)
        lock_path = self.state_dir / "active-sessions.lock"
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch("herdr_bartender.cache.lock_timeout_seconds", return_value=0.02):
                handle_pane_moved(move_payload, {}, bridge_url=self.mock_url)
                handle_pane_exited(exit_payload, {}, bridge_url=self.mock_url)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

        self.assertEqual(len(self.bridge.history), posts_before, "contended handlers never transmit before Step A")

        spool_files = sorted((self.state_dir / "spool").glob("*.json"))
        self.assertEqual(len(spool_files), 2)
        move_env = json.loads(spool_files[0].read_text())
        exit_env = json.loads(spool_files[1].read_text())

        self.assertIsNone(validate_envelope(move_env))
        self.assertIsNone(validate_envelope(exit_env))
        self.assertEqual(move_env["event_name"], PANE_MOVED)
        self.assertFalse(is_close_envelope(move_env), "pane.moved is not slimmed as a close envelope")
        self.assertEqual(move_env["event_data"]["previous_pane_id"], "w1:pMoveSrc")
        self.assertEqual(move_env["event_data"]["previous_workspace_id"], "w1")
        self.assertEqual(move_env["event_data"]["pane"]["pane_id"], "w2:pMoveDst")
        self.assertEqual(move_env["event_data"]["pane"]["foreground_cwd"], "/moved/fg")

        self.assertEqual(exit_env["event_name"], PANE_EXITED)
        self.assertTrue(is_close_envelope(exit_env), "pane.exited is a protected close envelope")
        self.assertIn(spool.CLOSE_MARK, spool_files[1].stem)

        batch = replay_spool_dir(self.state_dir)
        self.assertEqual(len(batch.quarantined), 0)
        self.assertEqual(len(batch.consumed), 2)
        self.assertEqual(
            set(batch.staged_sessions),
            {self.sid("w1:pMoveSrc"), self.sid("w2:pMoveDst"), self.sid("w1:pExitSrc")},
        )
        self.assertEqual(list((self.state_dir / "spool").glob("*.json")), [])
        self.assertFalse(self._marker_path("w1:pMoveSrc").exists())
        self.assertFalse(self._marker_path("w1:pExitSrc").exists())

        with self.cache_mgr as data:
            self.assertIn("w1:pMoveSrc", data["tombstones"])
            self.assertIn("w1:pExitSrc", data["tombstones"])
            self.assertEqual(data["sessions"][self.sid("w1:pMoveSrc")]["desired_payload"]["event"], "pane.moved")
            self.assertEqual(data["sessions"][self.sid("w1:pExitSrc")]["desired_payload"]["event"], "pane.exited")
            dst = data["sessions"][self.sid("w2:pMoveDst")]
            self.assertEqual(
                (dst["desired_state"], dst["agent"], dst["cwd"], dst["title"], dst["tab_id"], dst["desired_payload"]["event"]),
                ("Working", "Antigravity (Herdr)", "/moved/fg", "Moved Under Contention", "w2:t5", "pane.moved"),
            )

        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        new_posts = self.bridge.history[posts_before:]
        by_sid = {p["session_id"]: p for p in new_posts}
        self.assertEqual((by_sid[self.sid("w1:pMoveSrc")]["state"], by_sid[self.sid("w1:pMoveSrc")]["event"]),
                         ("Ended", "pane.moved"))
        self.assertEqual((by_sid[self.sid("w1:pExitSrc")]["state"], by_sid[self.sid("w1:pExitSrc")]["event"]),
                         ("Ended", "pane.exited"))
        self.assertEqual(
            (by_sid[self.sid("w2:pMoveDst")]["state"], by_sid[self.sid("w2:pMoveDst")]["agent"],
             by_sid[self.sid("w2:pMoveDst")]["event"]),
            ("Working", "Antigravity (Herdr)", "pane.moved"),
        )
        self.assertTrue(self._marker_path("w2:pMoveDst").exists())

    def test_pane_exited_protected_from_status_cap_and_evicts_irrelevant_close_at_ceiling(self):
        """pane.exited is protected from status-cap pruning and treated as relevant in spool_overflow."""
        # 1. Protected from SPOOL_CAP status eviction
        exit_path = enqueue_spool(PANE_EXITED, {"pane_id": "w1:pKeep", "workspace_id": "w1"}, {},
                                  arrival_ns=time.time_ns())
        for i in range(SPOOL_CAP + 5):
            enqueue_spool(
                "pane.agent_status_changed",
                {"pane_id": f"w1:pS{i}", "workspace_id": "w1", "agent": "claude", "agent_status": "working"},
                {},
            )
        self.assertTrue(exit_path.exists(), "pane.exited close envelope is never pruned by status cap")

        # 2. At CLOSE_HARD_CAP, a pane.exited targeting a destination pane of a spooled pane.moved evicts an irrelevant close
        spool_dir = self.state_dir / "spool"
        for p in spool_dir.glob("*.json"):
            p.unlink()
        enqueue_spool(
            PANE_MOVED,
            {
                "previous_pane_id": "w1:pOld",
                "previous_workspace_id": "w1",
                "pane": {"pane_id": "w2:pMovedLive", "workspace_id": "w2", "agent": "agy", "agent_status": "working"},
            },
            {},
        )
        with mock.patch.object(spool, "CLOSE_HARD_CAP", 2):
            stale = [enqueue_spool("pane.closed", {"pane_id": f"w9:gone{n}", "workspace_id": "w9"}, {}) for n in range(2)]
            relevant_exit = enqueue_spool(PANE_EXITED, {"pane_id": "w2:pMovedLive", "workspace_id": "w2"}, {})
        self.assertTrue(relevant_exit.exists())
        self.assertFalse(stale[0].exists(), "irrelevant close evicted to make room for relevant pane.exited")


if __name__ == "__main__":
    unittest.main()
