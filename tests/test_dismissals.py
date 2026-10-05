"""Reconciler side of dismissed_vendor_uuids and the .vendor_active horizons (Plan §5.1 item 5b; R11, R19).

Gaps: dismissal-cadence, t60-cancel-triggers, t65-settling, plan-contradiction-dismissal-attempts,
vendor-active-12h-horizon-missing, t45-bare-60s.
"""

import json
import os
import time
import unittest

from herdr_bartender import dismissals, process
from herdr_bartender.cache_schema import DISMISSED_VENDOR_CAP
from herdr_bartender.dismissals import Triggers, plan_dismissals
from herdr_bartender.handlers import handle_agent_status_changed, handle_pane_closed
from herdr_bartender.reconciler import reconcile_active_sessions
from herdr_bartender.sanitize import get_hex_pane_id
from herdr_bartender.vendor import stage_dismissal
from tests.support import SandboxTestCase
from tests.support.reconciler_fixtures import LoopRunner, pane_file, read_cache, seed, session
from tests.support.sandbox import DEFAULT_BARTENDER_PID

UUID = "vendor-uuid-0000000065"
PANE = "w1:pDismiss"


class DismissalCase(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.clock = self.use_fake_clock()
        (self.state_dir / "panes").mkdir(parents=True, exist_ok=True)

    def queue(self, uuid=UUID, **entry):
        meta = {"timestamp": self.clock.time(), "pane_hex": get_hex_pane_id(PANE), "attempts": 1,
                "last_attempt": self.clock.time(), **entry}
        with self.cache_mgr as data:
            data["dismissed_vendor_uuids"][uuid] = meta
            self.cache_mgr.save(data)

    def queued(self):
        return read_cache(self.cache_mgr)["dismissed_vendor_uuids"]

    def posts(self, uuid=UUID):
        return [r for r in self.bridge.requests if r["method"] == "POST" and (r["body"] or {}).get("session_id") == uuid]

    def reconcile(self):
        reconcile_active_sessions(self.state_dir, bridge_url=self.mock_url)


class CancellationTests(DismissalCase):
    def _trigger(self, name):
        if name == ".vendor_active":
            pane_file(self.state_dir, PANE, ".vendor_active").write_text(json.dumps({"vendor_session_id": UUID}))
        elif name == ".failed":
            pane_file(self.state_dir, PANE, ".failed").write_text("1")
        elif name == "NO_HOOKS":
            (self.state_dir / "NO_HOOKS").touch()
        elif name == "herdr dead":
            self.clear_fake_processes()
            self.add_fake_process("Bartender 6", pid=DEFAULT_BARTENDER_PID)
            process.reset_caches()

    def test_p60_every_trigger_cancels_without_sending(self):
        """Plan §10.1 #60 (gap t60-cancel-triggers): .vendor_active, .failed, NO_HOOKS and Herdr dead (pgrep shim) each
        purge a queued dismissal in a reconciler pass without POSTing its Ended."""
        for trigger in (".vendor_active", ".failed", "NO_HOOKS", "herdr dead"):
            with self.subTest(trigger=trigger):
                self.queue(last_attempt=self.clock.time() - 3.0)
                self._trigger(trigger)
                self.reconcile()
                self.assertNotIn(UUID, self.queued())
                self.assertEqual(self.posts(), [])
                for leftover in (pane_file(self.state_dir, PANE, ".vendor_active"),
                                 pane_file(self.state_dir, PANE, ".failed"), self.state_dir / "NO_HOOKS"):
                    leftover.unlink(missing_ok=True)
                self.set_herdr_alive()
                process.reset_caches()

    def test_p60_disabled_cancels_without_sending(self):
        """Plan §10.1 #60, DISABLED trigger: the sweep purges without POSTing (the loop itself also stops)."""
        self.queue(last_attempt=self.clock.time() - 3.0)
        (self.state_dir / "DISABLED").touch()
        sent = []
        dismissals.sweep_dismissals(self.cache_mgr, True, lambda uuids: sent.append(uuids))
        self.assertEqual((self.queued(), sent), ({}, []))

    def test_pane_close_dismissal_ignores_pane_triggers_but_not_global_ones(self):
        """R24: pane-scoped triggers protect a live vendor fallback; a pane-close dismissal has none left."""
        live_close = Triggers(fallback_active=lambda _hex: True)
        entry = {"timestamp": 0.0, "pane_hex": "aa", "attempts": 0, "last_attempt": 0.0, "pane_closed": True}
        self.assertEqual(dismissals.verdict(UUID, entry, 1.0, live_close), dismissals.DUE)
        self.assertEqual(dismissals.verdict(UUID, {**entry, "pane_closed": False}, 1.0, live_close),
                         dismissals.CANCEL)
        self.assertEqual(dismissals.verdict(UUID, entry, 1.0, Triggers(herdr_dead=True)), dismissals.CANCEL)

    def test_pane_close_marks_the_queued_dismissal(self):
        pane_file(self.state_dir, PANE, ".vendor_active").write_text(json.dumps({"vendor_session_id": UUID}))
        self.bridge.on_post = lambda p: self.bridge.enqueue(500) if p.get("session_id") == UUID else None
        handle_pane_closed({"pane_id": PANE}, {}, bridge_url=self.mock_url)
        self.assertIs(self.queued()[UUID]["pane_closed"], True)

    def test_p60_ttl_ended_dismissal_is_cancelled_when_vendor_fallback_returns(self):
        """Plan §5.1 5b / #60 with R24 narrowed: a confirmed Ended that is NOT a pane/tab/workspace close (here a TTL
        expiry; the pane and a vendor CLI may live on) queues a dismissal that the pane-scoped triggers still cancel:
        once the vendor hook re-creates .vendor_active, the queued Ended for that vendor UUID is purged unsent."""
        seed(self.cache_mgr, {self.sid(PANE): session(PANE, "Working", now=self.clock.time() - 43201)})
        vendor = pane_file(self.state_dir, PANE, ".vendor_active")
        vendor.write_text(json.dumps({"vendor_session_id": UUID}))
        self.bridge.on_post = lambda p: self.bridge.enqueue(500) if p.get("session_id") == UUID else None
        self.reconcile()
        self.assertEqual([e["state"] for e in self.bridge.events_for(self.sid(PANE))], ["Ended"])
        self.assertIs(self.queued()[UUID]["pane_closed"], False, "a TTL expiry is not a pane close")
        self.assertEqual(len(self.posts()), 1)
        vendor.write_text(json.dumps({"vendor_session_id": UUID}))   # live vendor fallback on the same pane
        self.clock.advance(2.0)
        self.reconcile()
        self.assertNotIn(UUID, self.queued())
        self.assertEqual(len(self.posts()), 1, "cancelled without sending")


class SettlingWindowTests(DismissalCase):
    def test_p65_two_second_gate_five_attempts_ten_second_purge(self):
        """Plan §10.1 #65 (gap t65-settling, R11): (a) 1.0s after the last attempt nothing is sent; (b) at 2.0s it is
        sent and counted; (c) the 5th unconfirmed attempt purges it; (d) an entry 10s old is purged unsent;
        (e) a confirmed 200 purges it at once."""
        self.bridge.on_post = lambda p: self.bridge.enqueue(500) if p.get("session_id") == UUID else None
        self.queue(attempts=1, last_attempt=self.clock.time() - 1.0)
        self.reconcile()
        self.assertEqual((len(self.posts()), self.queued()[UUID]["attempts"]), (0, 1), "(a) 2.0s gate")
        self.clock.advance(1.0)
        self.reconcile()
        self.assertEqual((len(self.posts()), self.queued()[UUID]["attempts"]), (1, 2), "(b) due at 2.0s")
        self.queue(attempts=4, last_attempt=self.clock.time() - 2.0)
        self.reconcile()
        self.assertNotIn(UUID, self.queued(), "(c) purged at its 5th attempt")
        self.assertEqual(len(self.posts()), 2)
        self.queue(attempts=5, last_attempt=self.clock.time() - 5.0)
        self.reconcile()
        self.assertEqual((self.queued(), len(self.posts())), ({}, 2), "(c') attempts == 5: purged unsent")
        self.queue(attempts=1, timestamp=self.clock.time() - 10.0, last_attempt=self.clock.time() - 3.0)
        self.reconcile()
        self.assertEqual((self.queued(), len(self.posts())), ({}, 2), "(d) 10s window")
        self.bridge.on_post = None
        self.queue(attempts=1, last_attempt=self.clock.time() - 2.0)
        self.reconcile()
        self.assertEqual((self.queued(), self.posts()[-1]["body"]),
                         ({}, {"state": "Ended", "agent": "Herdr", "session_id": UUID}), "(e) purged on 200")

    def test_loop_sends_at_0_2_4_6_8_then_purges(self):
        """Gap dismissal-cadence: the reconciler loop wakes for due dismissals (0.5s ticks + exact due times) and sends
        exactly five unconfirmed attempts 2.0s apart, then purges the entry and idles out."""
        start = self.clock.time()
        with self.cache_mgr as data:
            stage_dismissal(data, UUID, PANE, start)
            self.cache_mgr.save(data)
        sent_at = []

        def unconfirmed(payload):
            if payload.get("session_id") == UUID:
                sent_at.append(round(self.clock.time() - start, 3))
                self.bridge.enqueue(500)

        self.bridge.on_post = unconfirmed
        runner = LoopRunner(self.state_dir)
        runner.run(bridge_url=self.mock_url)  # ends by the 60s idle exit once the entry is purged
        self.assertEqual(sent_at, [0.0, 2.0, 4.0, 6.0, 8.0])
        self.assertEqual(self.queued(), {})
        self.assertFalse((self.state_dir / "DISABLED").exists(), "the loop idled out on its own")

    def test_queue_is_capped_at_64_oldest_pruned(self):
        with self.cache_mgr as data:
            for i in range(DISMISSED_VENDOR_CAP + 6):
                stage_dismissal(data, f"vendor-uuid-{i:016d}", PANE, 1000.0 + i)
            self.cache_mgr.save(data)
        queued = self.queued()
        self.assertEqual(len(queued), DISMISSED_VENDOR_CAP)
        self.assertNotIn(f"vendor-uuid-{0:016d}", queued)
        self.assertIn(f"vendor-uuid-{DISMISSED_VENDOR_CAP + 5:016d}", queued)

    def test_entry_without_timestamp_is_stamped_then_ages_out(self):
        queue = {UUID: {"pane_hex": "aa", "attempts": 1, "last_attempt": 0.0}}
        updated, due, _ = plan_dismissals(queue, 50.0, Triggers())
        self.assertEqual((updated[UUID]["timestamp"], due), (50.0, (UUID,)))
        later, _, dropped = plan_dismissals(updated, 60.0, Triggers())
        self.assertEqual((later, dropped), ({}, 1))


class VendorActiveHorizonTests(DismissalCase):
    def _vendor_file(self, pane, uuid=None, age=0.0):
        path = pane_file(self.state_dir, pane, ".vendor_active")
        path.write_text(json.dumps({"vendor_session_id": uuid}) if uuid else "")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def test_uuid_vendor_active_older_than_12h_is_dismissed_and_retired(self):
        """Plan §5.1 item 5b (gap vendor-active-12h-horizon-missing): a UUID record untouched for >12h is dismissed
        (agent Herdr) and removed; one 11h59m old and any bare touch are kept."""
        old = self._vendor_file("w1:pOld", "vendor-uuid-old-0000001", age=43201)
        young = self._vendor_file("w1:pYoung", "vendor-uuid-young-00001", age=43199)
        bare = self._vendor_file("w1:pBare", age=50000)
        self.reconcile()
        self.assertFalse(old.exists())
        self.assertTrue(young.exists() and bare.exists())
        self.assertEqual([r["body"] for r in self.posts("vendor-uuid-old-0000001")],
                         [{"state": "Ended", "agent": "Herdr", "session_id": "vendor-uuid-old-0000001"}])
        self.assertEqual(self.posts("vendor-uuid-young-00001"), [])

    def test_never_swept_while_herdr_is_dead(self):
        old = self._vendor_file("w1:pOld", "vendor-uuid-old-0000001", age=90000)
        self.clear_fake_processes()
        self.add_fake_process("Bartender 6", pid=DEFAULT_BARTENDER_PID)
        process.reset_caches()
        self.reconcile()
        self.assertTrue(old.exists())
        self.assertEqual(self.posts("vendor-uuid-old-0000001"), [])

    def test_p45_bare_touch_survives_60s_then_pane_close_removes_it(self):
        """Plan §10.1 #45 (gap t45-bare-60s): a bare .vendor_active aged 120s while the session is active survives a
        reconcile pass (no blind expiry); the pane close then removes it."""
        pane = "w1:pBare45"
        seed(self.cache_mgr, {self.sid(pane): session(pane, now=self.clock.time())})
        bare = self._vendor_file(pane, age=120)
        self.reconcile()
        self.assertTrue(bare.exists(), "no blind expiry of a bare touch")
        handle_pane_closed({"pane_id": pane}, {}, bridge_url=self.mock_url)
        self.assertFalse(bare.exists())

    def test_confirmed_delivery_unlinks_a_bare_touch(self):
        pane = "w1:pBareDeliv"
        bare = self._vendor_file(pane, age=120)
        handle_agent_status_changed({"agent_status": "working", "pane_id": pane, "workspace_id": "w1",
                                     "agent": "claude"}, {}, bridge_url=self.mock_url)
        self.assertFalse(bare.exists())


if __name__ == "__main__":
    unittest.main()
