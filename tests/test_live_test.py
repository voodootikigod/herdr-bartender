"""--live-test / --test (Plan §10.2 item 1), exercised against the sandbox mock bridge only.

The live test posts Working, Waiting, Done, Idle and then Ended for a session id of its own,
requires ``{"ok": true}`` for every POST, and requires ``/health`` ``sessions`` to return to
its baseline after the Ended. Nothing here reaches the real bridge: the sandbox points
NOTCHBAR_AGENTS_PORT at the mock and registers a fake Bartender process for the liveness gate.
"""

import contextlib
import io
import sys
import unittest
from unittest import mock

from herdr_bartender import cli, live_test, runtime
from herdr_bartender.config import SESSION_ID_REGEX
from tests.support import SandboxTestCase

STATES = ["Working", "Waiting", "Done", "Idle", "Ended"]


class LiveTestBase(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.clock = self.use_fake_clock()   # step pauses and settle polling advance fake time

    def run_live(self, **kwargs):
        out = io.StringIO()
        code = live_test.run_live_test(out=out, **kwargs)
        return code, out.getvalue()

    def live_posts(self):
        return [p for p in self.bridge.posts() if isinstance(p, dict)]


class LiveTestPassTests(LiveTestBase):
    def test_full_sequence_passes_against_mock_bridge(self):
        """Plan §10.2 item 1: Working/Waiting/Done/Idle then Ended, each ok:true; /health back to baseline; exit 0."""
        code, report = self.run_live()
        self.assertEqual(code, 0, report)
        posts = self.live_posts()
        self.assertEqual([p["state"] for p in posts], STATES)
        sids = {p["session_id"] for p in posts}
        self.assertEqual(len(sids), 1)
        self.assertEqual(self.bridge.sessions, {})
        self.assertIn("RESULT: PASS", report)
        self.assertNotIn("[FAIL]", report)
        self.assertEqual(report.count("[PASS]"), 7)   # baseline + 5 POSTs + baseline return

    def test_baseline_counts_sessions_already_on_top_shelf(self):
        """The baseline is whatever /health reports first: unrelated live sessions stay and the check still passes."""
        self.bridge.sessions["claude-native-uuid-0001"] = {"state": "Working"}
        code, report = self.run_live()
        self.assertEqual(code, 0, report)
        self.assertIn("baseline sessions = 1", report)
        self.assertEqual(list(self.bridge.sessions), ["claude-native-uuid-0001"])

    def test_session_id_is_unique_valid_and_never_a_herdr_pane(self):
        """Each run uses a fresh id matching SESSION_ID_REGEX under the hb-livetest prefix (no real pane collides)."""
        self.run_live()
        self.run_live()
        sids = [p["session_id"] for p in self.live_posts()]
        first, second = sids[0], sids[-1]
        self.assertNotEqual(first, second)
        for sid in (first, second):
            self.assertRegex(sid, SESSION_ID_REGEX)
            self.assertTrue(sid.startswith(f"herdr:{self.host}:{live_test.LIVE_PANE_PREFIX}:"), sid)

    def test_status_payloads_match_the_bridge_contract(self):
        """Status POSTs carry state/agent/session_id/title/terminal; the Ended POST is the minimal shape."""
        self.run_live()
        posts = self.live_posts()
        for p in posts[:4]:
            self.assertEqual(set(p), {"state", "agent", "session_id", "title", "terminal"})
            self.assertEqual(p["terminal"], "Herdr")
        self.assertEqual(set(posts[-1]), {"state", "agent", "session_id"})

    def test_steps_are_paused_for_a_human_observer(self):
        """The default run pauses STEP_PAUSE_SECONDS after each status POST (on the injectable clock)."""
        self.run_live()
        self.assertEqual(self.clock.sleeps.count(live_test.STEP_PAUSE_SECONDS), 4)

    def test_never_touches_the_plugin_session_cache(self):
        """The live test talks to the bridge only: the plugin cache stays empty and no reconciler is spawned."""
        self.run_live()
        with self.cache_mgr as data:
            self.assertEqual(data.get("sessions"), {})
        self.assertEqual(self.spawner.calls, [])

    def test_runs_unbounded_by_the_event_path_deadline(self):
        """Requests are not squeezed by the 1.5s event budget: the live test runs in DEADLINE_UNBOUNDED."""
        modes = []
        real_send = live_test.send_event

        def spy(payload, **kwargs):
            modes.append(runtime.deadline_bounded())
            return real_send(payload, **kwargs)

        with mock.patch.object(live_test, "send_event", spy):
            code, _ = self.run_live()
        self.assertEqual(code, 0)
        self.assertEqual(modes, [False] * 5)


class LiveTestFailTests(LiveTestBase):
    def test_bridge_unreachable_fails_without_posting(self):
        """No answer on /health: one FAIL line, no POST at all, exit 1."""
        self.bridge.stop()
        code, report = self.run_live()
        self.assertEqual(code, 1)
        self.assertIn("RESULT: FAIL", report)
        self.assertEqual(self.bridge.posts(), [])

    def test_bartender_not_running_fails_without_posting(self):
        """The liveness gate holds for the live test too: no Bartender process, no request to the listener."""
        self.clear_fake_processes()
        code, report = self.run_live()
        self.assertEqual(code, 1)
        self.assertEqual(self.bridge.requests, [])
        self.assertIn("[FAIL]", report)

    def test_health_without_session_count_fails(self):
        """§3.1 /health carries an integer sessions count; without it the baseline check cannot be made."""
        with mock.patch.object(live_test, "check_bridge_health", return_value={"ok": True, "port": 7823}):
            code, report = self.run_live()
        self.assertEqual(code, 1)
        self.assertEqual(self.bridge.posts(), [])
        self.assertIn("RESULT: FAIL", report)

    def test_ok_false_on_a_status_post_fails_and_still_sends_ended(self):
        """HTTP 200 {"ok": false} is a failure; the run stops the sequence but still dismisses its entry."""
        self.bridge.enqueue(200, {"ok": True})
        self.bridge.enqueue(200, {"ok": False})
        code, report = self.run_live()
        self.assertEqual(code, 1)
        self.assertEqual([p["state"] for p in self.live_posts()], ["Working", "Waiting", "Ended"])
        self.assertRegex(report, r"\[FAIL\] POST Waiting -> bridge_rejected")
        self.assertIn("RESULT: FAIL", report)

    def test_rejected_ended_fails_without_minimal_retry(self):
        """A 5xx on Ended fails the run; the live test sends each POST once (nothing hides a rejection)."""
        for _ in range(4):
            self.bridge.enqueue(200, {"ok": True})
        self.bridge.enqueue(503, b"")
        code, report = self.run_live()
        self.assertEqual(code, 1)
        self.assertEqual([p["state"] for p in self.live_posts()], STATES)
        self.assertRegex(report, r"\[FAIL\] POST Ended -> 5xx_server_error \(HTTP 503\)")

    def test_session_count_not_back_to_baseline_fails(self):
        """Ended answered ok:true but /health still counts the session after the settle window: FAIL."""
        def keep_session(payload):
            if isinstance(payload, dict) and payload.get("state") == "Ended":
                self.bridge.sessions[payload["session_id"]] = payload

        self.bridge.after_apply = keep_session
        code, report = self.run_live(settle=1.0)
        self.assertEqual(code, 1)
        self.assertRegex(report, r"\[FAIL\] /health sessions 1 did not return to baseline 0")
        self.assertGreaterEqual(self.clock.sleeps.count(live_test.SETTLE_POLL_SECONDS), 4)

    def test_late_baseline_return_within_settle_window_passes(self):
        """Bartender may apply the Ended asynchronously: a count that settles within the window passes."""
        polls = {"n": 0}
        real_health = live_test.check_bridge_health

        def lagging_health(**kwargs):
            health = real_health(**kwargs)
            polls["n"] += 1
            if polls["n"] == 2 and isinstance(health, dict):   # first poll after Ended still sees it
                health = dict(health, sessions=health["sessions"] + 1)
            return health

        with mock.patch.object(live_test, "check_bridge_health", lagging_health):
            code, report = self.run_live()
        self.assertEqual(code, 0, report)
        self.assertGreaterEqual(polls["n"], 3)


class LiveTestCliTests(LiveTestBase):
    def _main(self, *args):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["herdr-bartender", *args]), contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                cli.main()
        return ctx.exception.code, out.getvalue()

    def test_live_test_flag_and_test_alias_exit_zero_on_pass(self):
        """--live-test and its alias --test both run the live check and exit with its code."""
        for flag in ("--live-test", "--test"):
            with self.subTest(flag=flag):
                code, report = self._main(flag)
                self.assertEqual(code, 0, report)
                self.assertIn("RESULT: PASS", report)
        self.assertEqual(len(self.live_posts()), 10)

    def test_live_test_flag_exits_one_on_fail(self):
        """A failed check propagates as exit 1 from main()."""
        self.bridge.enqueue(500, b"")
        code, report = self._main("--live-test")
        self.assertEqual(code, 1)
        self.assertIn("RESULT: FAIL", report)

    def test_subprocess_unreachable_bridge_reports_fail(self):
        """bin/herdr-bartender --live-test as a process: a dead bridge prints FAIL and exits 1 promptly."""
        self.bridge.stop()
        res = self.run_cli("--live-test")
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("RESULT: FAIL", res.stdout)
        self.assertNotIn("Traceback", res.stderr)


if __name__ == "__main__":
    unittest.main()
