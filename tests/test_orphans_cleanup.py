"""--replay-orphans and --cleanup contracts."""

import json
import time
import unittest
from unittest import mock

from herdr_bartender import cache, runtime
from herdr_bartender.bridge import DeliveryResult, post_bartender_event
from herdr_bartender.sender import step_b
from herdr_bartender.cleanup import run_cleanup
from herdr_bartender.markers import touch_pane_marker
from herdr_bartender.replay import run_replay_orphans
from herdr_bartender.paths import get_orphan_path
from tests.support import SandboxTestCase
from tests.support.lock_holder import hold_lock


class OrphanCleanupTests(SandboxTestCase):
    def test_p16_orphan_replay_clears_file(self):
        """Plan §10.1 #16: an exported orphan file is replayed and removed when the bridge is online."""
        orphan_file = self.state_dir / "test-orphans.json"
        orphan_file.write_text(json.dumps({"sessions": {"herdr:macbook:wOrphan": {
            "agent": "Claude (Herdr)", "pane_id": "w1:pOrphan"}}}))
        touch_pane_marker("w1:pOrphan")
        ok = run_replay_orphans(str(orphan_file), bridge_url=self.mock_url)
        self.assertIs(ok, True, "Orphan replay should succeed")
        self.assertFalse(orphan_file.exists(), "Orphan file should be removed after successful replay")

    def test_p25_cleanup_exit_codes_and_orphan_perms(self):
        """Plan §10.1 #25: --cleanup returns 2 and exports a 0600 orphan file when unreachable, 0 when reachable."""
        orphan_canonical = self.home / ".herdr-bartender-orphans.json"
        self.assertEqual(get_orphan_path(), orphan_canonical,
                         "orphan export must live at $HOME/.herdr-bartender-orphans.json (sandboxed HOME)")
        with self.cache_mgr as data:
            data["sessions"]["herdr:macbook:wOrphanCleanup"] = {
                "desired_state": "Working", "seq": 1, "delivered_seq": 1,
                "pane_id": "w1:pOrphanCleanup", "agent": "Claude (Herdr)", "last_event_at": time.time(),
            }
            self.cache_mgr.save(data)
        self.bridge.return_code = 500
        cleanup_code = run_cleanup(bridge_url=self.mock_url)
        self.assertEqual(cleanup_code, 2, f"Expected exit code 2 when bridge unreachable, got {cleanup_code}")
        self.assertTrue(orphan_canonical.exists(), f"Orphans must be exported to {orphan_canonical}")
        self.assertEqual(oct(orphan_canonical.stat().st_mode)[-3:], "600", "Orphan file must have 0600 permissions")
        orphan_canonical.unlink()

        self.bridge.return_code = 200
        cleanup_code_ok = run_cleanup(bridge_url=self.mock_url)
        self.assertEqual(cleanup_code_ok, 0, f"Expected exit code 0 when bridge reachable, got {cleanup_code_ok}")

    def test_p29_ended_minimal_retry_and_retention(self):
        """Plan §10.1 #29: a rejected Ended retries with a minimal payload; an unclearable session is kept and exported."""
        self.bridge.reject_complex_ended = True
        complex_ended_payload = {
            "state": "Ended",
            "agent": "Claude (Herdr)",
            "session_id": "herdr:mock:complex1",
            "title": "Complex Title",
            "extra_info": "Should be stripped on retry",
        }
        succ, _is_non_ret = post_bartender_event(complex_ended_payload, bridge_url=self.mock_url)
        self.assertIs(succ, True, "post_bartender_event must succeed by retrying with minimal payload")
        self.bridge.reject_complex_ended = False

        orphan_canonical = get_orphan_path()
        with self.cache_mgr as data:
            data["sessions"]["herdr:mock:unclearable"] = {
                "desired_state": "Ended", "seq": 2, "delivered_seq": 1,
                "pane_id": "w1:pUnclearable", "agent": "Claude (Herdr)", "last_event_at": time.time(),
            }
            self.cache_mgr.save(data)
        self.bridge.return_code = 400
        cleanup_code_ret = run_cleanup(bridge_url=self.mock_url)
        self.assertEqual(cleanup_code_ret, 2, f"run_cleanup must return 2 when session cannot be cleared, got {cleanup_code_ret}")
        with self.cache_mgr as data:
            self.assertIn("herdr:mock:unclearable", data.get("sessions", {}), "Rejected Ended session must NOT be evicted from cache")
        self.assertTrue(orphan_canonical.exists(), "Rejected session must be preserved in orphan file")

    def test_p36_orphan_replay_generation_guard(self):
        """Plan §10.1 #36: replay skips the Ended for a pane whose cached session is newer and live."""
        orphan_gen_file = self.state_dir / "test-gen-orphans.json"
        sid_orphan = self.sid("w1:pGenOrphan")
        orphan_gen_file.write_text(json.dumps({"version": 1, "sessions": {sid_orphan: {
            "session_id": sid_orphan, "pane_id": "w1:pGenOrphan", "generation": 1, "agent": "Claude (Herdr)"}}}),
            encoding="utf-8")
        with self.cache_mgr as data:
            data["sessions"][sid_orphan] = {
                "generation": 2, "seq": 3, "delivered_seq": 3,
                "desired_state": "Working", "delivered_state": "Working",
                "pane_id": "w1:pGenOrphan", "agent": "Claude (Herdr)", "last_event_at": time.time(),
            }
            self.cache_mgr.save(data)
        self.bridge.history.clear()
        ok_replay = run_replay_orphans(str(orphan_gen_file), bridge_url=self.mock_url)
        self.assertIs(ok_replay, True)
        ended_dispatched = any(
            h.get("session_id") == sid_orphan and h.get("state") == "Ended" for h in self.bridge.history
        )
        self.assertFalse(ended_dispatched, "Orphan replay must NOT dispatch Ended for session superseded by newer generation")



class CleanupLockTests(SandboxTestCase):
    """--cleanup under the raising cache lock (finding: cleanup inherits the 0.2s event-path lock budget)."""

    def setUp(self):
        super().setUp()
        self.sid_ = self.sid("w1:pCleanLock")
        with self.cache_mgr as data:
            data["sessions"][self.sid_] = {"desired_state": "Working", "seq": 1, "delivered_seq": 1,
                                           "pane_id": "w1:pCleanLock", "agent": "Claude (Herdr)",
                                           "last_event_at": time.time()}
            self.cache_mgr.save(data)
        runtime.PROCESS_DEADLINE_SECONDS = runtime.DEFAULT_DEADLINE_SECONDS
        runtime.START_TIME = time.monotonic()  # inside the first 1.5s: the event-path budget would apply

    def test_cleanup_is_unbounded_and_waits_out_brief_contention(self):
        """Plan L619/L686 + R10: --cleanup is exempt from the 1.5s event budget; a lock held for 0.5s by another
        process delays it instead of failing it with exit 1."""
        hold_lock(self, self.cache_mgr.lock_file, seconds=0.5)
        bounded_while_sending = []
        self.bridge.on_post = lambda _payload: bounded_while_sending.append(runtime.deadline_bounded())
        self.assertEqual(run_cleanup(bridge_url=self.mock_url), 0)
        self.assertEqual(bounded_while_sending, [False], "the Ended is sent in the unbounded deadline mode")
        with self.cache_mgr as data:
            self.assertNotIn(self.sid_, data["sessions"])

    def test_confirmation_is_kept_when_the_cache_cannot_be_relocked(self):
        """Plan §4.3 L483 applied to --cleanup: a POST confirmed but not recordable under the lock goes to results/
        (the reconciler applies it) instead of being lost behind exit 1."""
        real_flock, posted = cache._flock_within, []

        def post(payload, timeout=0.2, bridge_url=None):
            posted.append(payload)
            return DeliveryResult("success", None, 200)

        def flock(fd, timeout):
            return False if posted else real_flock(fd, timeout)

        with mock.patch.object(step_b, "send_event", side_effect=post), \
                mock.patch.object(cache, "_flock_within", side_effect=flock):
            self.assertEqual(run_cleanup(bridge_url=self.mock_url), 0)
        (result,) = [json.loads(p.read_text()) for p in (self.state_dir / "results").glob("*.json")]
        self.assertEqual((result["session_id"], result["transmitting_state"], result["status"]),
                         (self.sid_, "Ended", "success"))


if __name__ == "__main__":
    unittest.main()
