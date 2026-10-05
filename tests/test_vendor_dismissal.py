"""Vendor dismissal: staged under the cache lock BEFORE it is sent, sent outside it (Plan §1 L9/L56-57, R11, R19).

Gaps: vendor-dismissal-send-before-stage (critic), t30-outside-lock, pane-closed-retryable (vendor cleanup
budgeted and persisted).
"""

import json
import os
import time
import unittest
from unittest import mock

from herdr_bartender import clock, runtime, spool, vendor
from herdr_bartender.handlers import handle_agent_status_changed, handle_pane_closed
from herdr_bartender.sanitize import get_hex_pane_id
from tests.support import SandboxTestCase
from tests.support.probes import cache_lock_probe

PANE = "w1:pVendor"
UUID = "vendor-session-uuid-0001"


class VendorCase(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.vendor_file = self.state_dir / "panes" / f"{get_hex_pane_id(PANE)}.vendor_active"
        self.vendor_file.parent.mkdir(parents=True, exist_ok=True)

    def _write_uuid(self, uuid=UUID):
        self.vendor_file.write_text(json.dumps({"vendor_session_id": uuid}))

    def _queue(self):
        with self.cache_mgr as data:
            return dict(data["dismissed_vendor_uuids"]), list(data["pending_vendor_cleanups"])

    def _dismissals(self, uuid=UUID):
        return [r for r in self.bridge.requests if (r["body"] or {}).get("session_id") == uuid]

    def _working(self, **extra):
        handle_agent_status_changed({"agent_status": "working", "pane_id": PANE, "workspace_id": "w1",
                                     "agent": "claude", **extra}, {}, bridge_url=self.mock_url)


class StageBeforeSendTests(VendorCase):
    def test_p30_dismissal_is_staged_then_sent_outside_the_lock(self):
        """Plan §10.1 #30: on confirmed delivery the UUID is queued in dismissed_vendor_uuids under the lock, then the
        Ended is POSTed with the lock free; the confirmed dismissal is purged (R11)."""
        self._write_uuid()
        seen_in_cache = []

        def observe(payload):
            if payload.get("session_id") == UUID:
                with self.cache_mgr as data:  # the lock is free while the dismissal is in flight
                    seen_in_cache.append(dict(data["dismissed_vendor_uuids"].get(UUID) or {}))

        self.bridge.probe = cache_lock_probe(self.cache_mgr.lock_file)
        self.bridge.on_post = observe
        self._working()
        (request,) = self._dismissals()
        self.assertEqual(request["body"], {"state": "Ended", "agent": "Herdr", "session_id": UUID})
        self.assertEqual((request["in_critical_section"], request["cache_lock_free"]), (False, True))
        self.assertEqual(seen_in_cache[0]["attempts"], 0, "staged (attempts 0) before the POST")
        self.assertEqual(seen_in_cache[0]["pane_hex"], get_hex_pane_id(PANE))
        self.assertFalse(self.vendor_file.exists())
        dismissed, pending = self._queue()
        self.assertEqual((dismissed, pending), ({}, []))

    def test_death_right_after_the_post_keeps_the_dismissal_owned(self):
        """Critic vendor-dismissal-send-before-stage: a process killed right after the POST leaves the queued
        dismissal in the cache for the reconciler (attempts 0), and .vendor_active is already gone."""
        self._write_uuid()

        def post_then_die(payload, timeout=None, bridge_url=None):
            raise SystemExit(0)

        with mock.patch.object(vendor, "send_event", side_effect=post_then_die):
            with self.assertRaises(SystemExit):
                self._working()
        dismissed, _ = self._queue()
        self.assertEqual(dismissed[UUID]["attempts"], 0)
        self.assertFalse(self.vendor_file.exists())
        with self.cache_mgr as data:
            self.assertEqual(data["sessions"][self.sid(PANE)]["delivery_status"], "delivered")

    def test_failed_dismissal_is_counted_for_the_reconciler(self):
        self._write_uuid()
        self.bridge.on_post = lambda p: self.bridge.enqueue(500) if p.get("session_id") == UUID else None
        self._working()
        dismissed, _ = self._queue()
        self.assertEqual(dismissed[UUID]["attempts"], 1)
        self.assertGreater(dismissed[UUID]["last_attempt"], 0)
        self.assertTrue((self.state_dir / "reconciler.pending").exists())

    def test_no_budget_leaves_the_dismissal_queued(self):
        self._write_uuid()
        self.bridge.after_apply = (lambda p: setattr(runtime, "PROCESS_DEADLINE_SECONDS", 0.2) or
                                   setattr(runtime, "START_TIME", clock.monotonic())
                                   if p.get("state") == "Working" else None)
        self._working()
        self.assertEqual(self._dismissals(), [])
        dismissed, _ = self._queue()
        self.assertEqual(dismissed[UUID]["attempts"], 0)
        self.assertFalse(self.vendor_file.exists())
        self.assertTrue((self.state_dir / "reconciler.pending").exists())

    def test_malformed_uuid_is_unlinked_like_a_bare_touch(self):
        self._write_uuid("short")
        self._working()
        self.assertFalse(self.vendor_file.exists())
        self.assertEqual(self._dismissals("short"), [])
        self.assertEqual(self._queue()[0], {})


class PaneCloseVendorTests(VendorCase):
    def test_pane_close_dismisses_a_vendor_only_pane(self):
        """Plan §1 L57 / §3.2: a pane Herdr never delivered (vendor fallback only) still has its vendor entry
        dismissed when the pane closes."""
        self._write_uuid()
        handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url)
        self.assertEqual(len(self._dismissals()), 1)
        self.assertFalse(self.vendor_file.exists())
        self.assertEqual(self._queue(), ({}, []))

    def test_pane_close_dismisses_even_when_the_herdr_ended_is_rejected(self):
        self._working()
        self._write_uuid()
        self.bridge.on_post = lambda p: self.bridge.enqueue(400) if p.get("session_id") == self.sid(PANE) else None
        handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url)
        self.assertEqual(len(self._dismissals()), 1)
        with self.cache_mgr as data:
            self.assertEqual(data["sessions"][self.sid(PANE)]["delivery_status"], "non_retryable_failed")

    def test_dual_teardown_pane_close_dismisses_after_a_rejected_container_close(self):
        """Plan §1 L103 / L249 (Dual Teardown) + R24, pinned at the final gate (review round 9 asked why a rejected
        tab-close Ended leaves the vendor entry): R24 queues a container close's dismissal only on a confirmed Ended,
        and Herdr follows every tab/workspace close with a ``pane.closed`` per enclosed pane, whose Step A queues the
        dismissal whatever happens to the session's own Ended - also for a session the tab close already ended."""
        from herdr_bartender.handlers import handle_tab_closed
        self._working(tab_id="w1:t1")
        self._write_uuid()
        self.bridge.on_post = lambda p: self.bridge.enqueue(400) if p.get("session_id") == self.sid(PANE) else None
        handle_tab_closed({"tab_id": "w1:t1", "workspace_id": "w1"}, {}, bridge_url=self.mock_url)
        self.assertEqual(self._dismissals(), [], "R24: the container close's Ended was not confirmed")
        handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url)
        self.assertEqual(len(self._dismissals()), 1, "the per-pane close of the dual teardown dismisses it")
        self.assertFalse(self.vendor_file.exists())

    def test_unreadable_record_is_never_retired(self):
        """Gate finding (review round 11): a ``.vendor_active`` that could not be read was retired with no rewrite
        check, so a valid UUID record the guard wrote after the failed read was deleted undismissed."""
        self._write_uuid()
        real_open = open

        def flaky_open(path, *args, **kwargs):
            if str(path) == str(self.vendor_file):
                raise PermissionError(13, "Permission denied")
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=flaky_open):
            record = vendor.read_vendor_file(self.vendor_file)
        self.assertIsNone(record.content, "control: the read failed")
        self._write_uuid("vendor-session-uuid-0002")   # the guard rewrites it meanwhile
        vendor.retire_vendor_file(record)
        self.assertEqual(json.loads(self.vendor_file.read_text())["vendor_session_id"], "vendor-session-uuid-0002")

    def test_stale_close_does_not_dismiss(self):
        arr = time.time_ns()
        handle_agent_status_changed({"agent_status": "working", "pane_id": PANE, "workspace_id": "w1",
                                     "agent": "claude"}, {}, bridge_url=self.mock_url, arrival_ns=arr)
        self._write_uuid()
        handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url, arrival_ns=arr - 1_000)
        self.assertTrue(self.vendor_file.exists())
        self.assertEqual(self._dismissals(), [])

    def test_replayed_close_and_persisted_cleanups_are_drained_by_the_next_event(self):
        """Plan §4.3 L486: a pane.closed replayed from the spool (or any persisted pending_vendor_cleanups entry)
        is resolved by the next event's Step A under the lock and its dismissal sent after it."""
        self._write_uuid()
        spool.enqueue_spool("pane.closed", {"pane_id": PANE}, {}, arrival_ns=time.time_ns())
        handle_agent_status_changed({"agent_status": "working", "pane_id": "w1:pOther", "workspace_id": "w1",
                                     "agent": "claude"}, {}, bridge_url=self.mock_url)
        self.assertEqual(len(self._dismissals()), 1)
        self.assertFalse(self.vendor_file.exists())
        self.assertEqual(self._queue(), ({}, []))

    def test_dropped_event_still_saves_the_persisted_cleanup_it_resolves(self):
        """Stage-before-send: a dropped event (stale source timestamp) changes no session, but resolving a persisted
        pending_vendor_cleanups entry queues a dismissal and unlinks .vendor_active, so Step A must save it. A failed
        dismissal is then counted, never lost."""
        gone = "w1:pGone"
        gone_file = self.state_dir / "panes" / f"{get_hex_pane_id(gone)}.vendor_active"
        self._working(timestamp=1000.0)
        with self.cache_mgr as data:
            data["pending_vendor_cleanups"] = [{"pane_id": gone, "is_pane_closed": True, "timestamp": time.time()}]
            self.cache_mgr.save(data)
        gone_file.write_text(json.dumps({"vendor_session_id": UUID}))
        self.bridge.on_post = lambda p: self.bridge.enqueue(500) if p.get("session_id") == UUID else None
        self._working(agent_status="blocked", timestamp=10.0)
        with self.cache_mgr as data:
            self.assertEqual(data["sessions"][self.sid(PANE)]["desired_state"], "Working", "the stale event dropped")
        dismissed, pending = self._queue()
        self.assertEqual((dismissed[UUID]["attempts"], pending), (1, []))
        self.assertFalse(gone_file.exists())

    def test_vendor_record_rewritten_after_it_was_read_is_kept(self):
        """TOCTOU: the guard rewrites .vendor_active without the cache lock. A record written after Step A read the
        old one is neither unlinked nor lost: only the record that was read (and queued) is retired."""
        self._write_uuid()
        newer = "vendor-session-uuid-0002"
        real_read = vendor.read_vendor_file

        def read_then_guard_rewrites(path):
            read = real_read(path)
            tmp = path.with_name(path.name + ".guard-tmp")
            tmp.write_text(json.dumps({"vendor_session_id": newer}))
            tmp.replace(path)  # the guard's mktemp + mv -f
            return read

        with mock.patch.object(vendor, "read_vendor_file", side_effect=read_then_guard_rewrites):
            handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url)
        self.assertEqual(len(self._dismissals()), 1, "the record that was read is dismissed")
        self.assertEqual(json.loads(self.vendor_file.read_text()), {"vendor_session_id": newer})
        self.assertEqual(sorted(p.name for p in self.vendor_file.parent.iterdir() if "vendor_active" in p.name),
                         [self.vendor_file.name], "no claimed leftovers")

    def test_identical_record_rewritten_or_refreshed_after_it_was_read_is_kept(self):
        """Low finding: the guard rewrites an UNCHANGED UUID record (mktemp + mv -f) and touches it on every unhealthy
        pass-through - the vendor fallback is live again. Comparing bytes alone could not see that, so the refreshed
        record was retired. The file identity read under the lock (inode, mtime) is compared too."""
        def claimed_leftovers():
            return [p.name for p in self.vendor_file.parent.iterdir() if vendor.CLAIM_INFIX in p.name]

        for label in ("rewrite", "touch"):
            with self.subTest(refresh=label):
                self._write_uuid()
                record = vendor.read_vendor_file(self.vendor_file)
                if label == "rewrite":
                    tmp = self.vendor_file.with_name(".va.tmp.guard")
                    tmp.write_bytes(record.content)
                    tmp.replace(self.vendor_file)
                else:
                    later = self.vendor_file.stat().st_mtime + 5
                    os.utime(self.vendor_file, (later, later))
                vendor.retire_vendor_file(record)
                self.assertTrue(self.vendor_file.exists(), "the refreshed record is put back")
                self.assertEqual(self.vendor_file.read_bytes(), record.content)
                self.assertEqual(claimed_leftovers(), [])
        untouched = vendor.read_vendor_file(self.vendor_file)
        vendor.retire_vendor_file(untouched)
        self.assertFalse(self.vendor_file.exists(), "control: the record that was read is retired")
        self.assertEqual(claimed_leftovers(), [])

    def test_compat_cleanup_stages_before_sending(self):
        """``cleanup_vendor_active`` (reconciler drain) follows the same stage -> send -> record order."""
        self._write_uuid()
        order = []

        def send(payload, timeout=None, bridge_url=None):
            order.append(("post", dict(self._queue()[0])))
            raise SystemExit(0)

        with mock.patch.object(vendor, "send_event", side_effect=send):
            with self.assertRaises(SystemExit):
                vendor.cleanup_vendor_active(PANE, is_pane_closed=True, bridge_url=self.mock_url)
        self.assertIn(UUID, order[0][1])
        self.assertEqual(self._queue()[0][UUID]["attempts"], 0)


class ReconcilerPaneCloseDismissalTests(VendorCase):
    """Gap reconciler/dismissal-cadence: the reconciler's dismissal drain uses dismiss_vendors (R11 purge on 200, R19
    agent "Herdr"). R24: a dismissal queued by a pane CLOSE is not cancelled by the pane-scoped triggers
    (.failed / .vendor_active) - the pane and its vendor CLI are gone, so there is no fallback representation left
    to protect, and cancelling would strand the vendor entry whenever the close's own Ended was rejected."""

    def test_pane_close_dismissal_left_to_the_reconciler_is_sent(self):
        """The pane's Ended is rejected (.failed touched) and the budget runs out before the inline dismissal: the
        queued dismissal is owed to the reconciler, which must send it (agent Herdr) and purge it on HTTP 200."""
        from herdr_bartender.reconciler import reconcile_active_sessions

        self._working()
        self._write_uuid()

        def reject_then_spend_the_budget(payload):
            if payload.get("session_id") == self.sid(PANE):
                self.bridge.enqueue(400)
                runtime.PROCESS_DEADLINE_SECONDS, runtime.START_TIME = 0.2, clock.monotonic()

        self.bridge.on_post = reject_then_spend_the_budget
        handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url)
        self.assertEqual(self._dismissals(), [])
        self.assertIn(UUID, self._queue()[0])
        runtime.PROCESS_DEADLINE_SECONDS, runtime.START_TIME = 60.0, clock.monotonic()
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)
        self.assertEqual([r["body"] for r in self._dismissals()],
                         [{"state": "Ended", "agent": "Herdr", "session_id": UUID}])
        self.assertEqual(self._queue()[0], {})


if __name__ == "__main__":
    unittest.main()
