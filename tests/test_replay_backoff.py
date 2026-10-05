"""Orphan replay: per-record backoff for the reconciler's automatic replay, the pane half of the skip guard, and the
commit's concurrency checks (Plan §9.2, §5.1 items 4 and 11).

Gaps (W4 review): an orphan the bridge keeps rejecting must not keep the reconciler alive forever nor be re-POSTed on
every 20s pass; the skip predicate's "same pane" lookup; a same-sid re-export during the POST; the marker of a pane a
different live session took over during the POST.
"""

import json
import unittest

from herdr_bartender import replay
from herdr_bartender.markers import touch_pane_marker
from herdr_bartender.orphans import export_orphan_record
from herdr_bartender.paths import get_orphan_path
from herdr_bartender.replay import replay_backoff, replay_due, run_replay_orphans
from tests.support import SandboxTestCase
from tests.support.reconciler_fixtures import LoopRunner, pane_file, read_cache, seed, session


class ReplayCase(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.clock = self.use_fake_clock()
        self.path = get_orphan_path()

    def write(self, records):
        self.path.write_text(json.dumps({"version": 1, "sessions": records}))

    def remaining(self):
        return json.loads(self.path.read_text())["sessions"] if self.path.exists() else {}

    def replay(self, **kwargs):
        return run_replay_orphans(str(self.path), bridge_url=self.mock_url, quiet=True, **kwargs)


class BackoffScheduleTests(ReplayCase):
    def test_backoff_doubles_from_20s_and_caps_at_300s(self):
        self.assertEqual([replay_backoff(n) for n in range(1, 7)], [20.0, 40.0, 80.0, 160.0, 300.0, 300.0])

    def test_due_rules(self):
        now = 1000.0
        self.assertTrue(replay_due({}, now), "never attempted")
        self.assertFalse(replay_due({"replay_attempts": 2, "last_replay_at": now - 39.0}, now))
        self.assertTrue(replay_due({"replay_attempts": 2, "last_replay_at": now - 40.0}, now))
        self.assertTrue(replay_due({"replay_attempts": 2, "last_replay_at": now + 3600.0}, now), "clock stepped back")
        self.assertTrue(replay_due("not a record", now), "never attempted")
        self.assertFalse(replay_due(replay.with_attempt("not a record", now), now), "backs off like any record")

    def test_unconfirmed_attempt_is_recorded_and_a_fresh_export_resets_it(self):
        sid = self.sid("w1:pRej")
        self.write({sid: {"agent": "Herdr", "pane_id": "w1:pRej"}})
        self.bridge.return_code = 400
        self.assertIs(self.replay(), False)
        record = self.remaining()[sid]
        self.assertEqual((record["replay_attempts"], record["last_replay_at"]), (1, self.clock.time()))
        self.assertIs(self.replay(), False)
        self.assertEqual(self.remaining()[sid]["replay_attempts"], 2, "--replay-orphans ignores the backoff")
        export_orphan_record(sid, {"agent": "Herdr", "pane_id": "w1:pRej"})
        self.assertNotIn("replay_attempts", self.remaining()[sid], "new export data starts a new schedule")

    def test_automatic_replay_skips_records_in_backoff(self):
        due, waiting = self.sid("w1:pDue"), self.sid("w1:pWait")
        now = self.clock.time()
        self.write({due: {"agent": "Herdr", "replay_attempts": 1, "last_replay_at": now - 20.0},
                    waiting: {"agent": "Herdr", "replay_attempts": 1, "last_replay_at": now - 5.0}})
        self.replay(only_due=True)
        self.assertEqual([e["session_id"] for e in self.bridge.history], [due])
        self.assertEqual(list(self.remaining()), [waiting])


class ReconcilerReplayBackoffTests(ReplayCase):
    def test_rejected_orphan_backs_off_and_lets_the_loop_idle_out(self):
        """Plan §5.1 item 11: with /health ok and 0 sessions the loop idles out after 60s even though an orphan the
        bridge rejects stays in the file (recoverable by --replay-orphans); it was POSTed a bounded number of times."""
        sid = self.sid("w1:pStuck")
        self.write({sid: {"agent": "Claude (Herdr)", "pane_id": "w1:pStuck"}})
        self.bridge.return_code = 400
        start = self.clock.time()
        runner = LoopRunner(self.state_dir, stop=lambda: self.clock.time() - start > 600)
        runner.run(bridge_url=self.mock_url)
        self.assertFalse((self.state_dir / "DISABLED").exists(), "the loop idled out on its own")
        self.assertLess(self.clock.time() - start, 120)
        record = self.remaining()[sid]
        self.assertGreaterEqual(record["replay_attempts"], 1)
        self.assertLessEqual(len(self.bridge.posts()), 2 * record["replay_attempts"])
        self.assertLessEqual(record["replay_attempts"], 3)


class SkipGuardPaneTests(ReplayCase):
    def test_live_session_with_another_sid_on_the_same_pane_suppresses_the_replay(self):
        """Plan §9.2 item 3: the guard looks the session id up, then the pane."""
        old = "herdr:oldhost:w1:pX"
        live = self.sid("w1:pX")
        seed(self.cache_mgr, {live: session("w1:pX", now=self.clock.time())})
        before = read_cache(self.cache_mgr)["sessions"][live]
        self.write({old: {"agent": "Herdr", "pane_id": "w1:pX"}})
        self.assertIs(self.replay(), True)
        self.assertEqual(self.bridge.history, [])
        self.assertFalse(self.path.exists(), "the skipped record is popped from the file")
        self.assertEqual(read_cache(self.cache_mgr)["sessions"][live], before)


class CommitConcurrencyTests(ReplayCase):
    def test_same_sid_reexported_during_the_post_is_kept(self):
        sid = self.sid("w1:pSame")
        self.write({sid: {"agent": "Herdr", "seq": 1}})
        self.bridge.on_post = lambda _p: export_orphan_record(sid, {"agent": "Herdr", "seq": 9})
        self.assertIs(self.replay(), True)
        self.assertEqual(self.remaining(), {sid: {"agent": "Herdr", "seq": 9}})

    def test_marker_of_a_pane_taken_over_during_the_post_survives(self):
        """Gap replay-marker-resync: the confirmed orphan's pane now hosts a different live session: it is re-synced and
        keeps its marker."""
        old, live = "herdr:oldhost:w1:pP", self.sid("w1:pP")
        self.write({old: {"agent": "Herdr", "pane_id": "w1:pP"}})

        def take_over(_payload):
            seed(self.cache_mgr, {live: session("w1:pP", now=self.clock.time())})
            touch_pane_marker("w1:pP")

        self.bridge.on_post = take_over
        self.assertIs(self.replay(), True)
        self.assertTrue(pane_file(self.state_dir, "w1:pP").exists())
        self.assertEqual(read_cache(self.cache_mgr)["sessions"][live]["delivery_status"], "in_flight", "re-synced")

    def test_capacity_is_256(self):
        self.assertEqual(replay.REPLAY_CAPACITY, 256)


if __name__ == "__main__":
    unittest.main()
