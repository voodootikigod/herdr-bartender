"""Round-4 low findings on the CLI's operator surface.

* ``--health`` printed ``{"error": "unreachable"}`` whatever the cause, so "Bartender is not running" (the
  process gate refused: nothing was sent) looked like "the bridge is down". The reason is now reported.
* A misspelled or unknown option (``--install-hook``, ``--help``) fell through to the event path and exited 0
  with no output. Herdr only ever passes an event name, so an option that is no command now prints usage
  (``--help``/``-h``: exit 0; anything else: exit 2 on stderr) and touches nothing.
"""

import json
import unittest

from herdr_bartender.bridge import check_bridge_health, health_report
from tests.support import SandboxTestCase


class HealthReasonTests(SandboxTestCase):
    def _health(self):
        res = self.run_cli("--health")
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_healthy_bridge_prints_its_health_json(self):
        report = self._health()
        self.assertTrue(report.get("ok"), report)
        self.assertNotIn("error", report)
        self.assertIn("hooks_guard_intact", report)

    def test_bartender_not_running_is_named_not_reported_as_unreachable(self):
        self.clear_fake_processes()   # no Bartender process: the liveness gate refuses before any request
        before = len(self.bridge.requests)
        report = self._health()
        self.assertEqual(report["error"], "bartender_not_running")
        self.assertIn("hooks_guard_intact", report)
        self.assertEqual(len(self.bridge.requests), before, "the gate refused: nothing was sent")

    def test_bridge_down_with_bartender_running_is_unreachable(self):
        self.bridge.stop()
        report = self._health()
        self.assertEqual(report["error"], "unreachable")

    def test_non_loopback_bridge_url_is_named(self):
        self.assertEqual(health_report(bridge_url="http://example.com:7823"), {"error": "invalid_bridge_url"})
        self.assertIsNone(check_bridge_health(bridge_url="http://example.com:7823"), "legacy API unchanged")


class UnknownOptionTests(SandboxTestCase):
    start_bridge = False

    def _assert_untouched(self):
        self.assertFalse((self.state_dir / "active-sessions.json").exists())
        self.assertEqual(self.subprocess_spawns(), [], "no reconciler is started for a typo")

    def test_misspelled_option_is_a_usage_error(self):
        for option in ("--install-hook", "--statu", "--foreground", "-x"):
            with self.subTest(option=option):
                res = self.run_cli(option)
                self.assertEqual(res.returncode, 2, res.stderr)
                self.assertIn(f"unknown option {option!r}", res.stderr)
                self.assertIn("usage: herdr-bartender", res.stderr)
                self.assertEqual(res.stdout, "")
        self._assert_untouched()

    def test_help_prints_usage_and_exits_0(self):
        for option in ("--help", "-h"):
            with self.subTest(option=option):
                res = self.run_cli(option)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn("usage: herdr-bartender", res.stdout)
                self.assertIn("--install-hooks", res.stdout)
        self._assert_untouched()

    def test_unknown_option_is_reported_even_while_disabled(self):
        (self.state_dir).mkdir(parents=True, exist_ok=True)
        (self.state_dir / "DISABLED").touch()
        res = self.run_cli("--install-hook")
        self.assertEqual(res.returncode, 2)

    def test_event_names_still_take_the_event_path(self):
        """Control: a non-option argv (Herdr's event name) is never a usage error."""
        res = self.run_cli("pane.closed", input=b"")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn(b"unknown option", res.stderr)


if __name__ == "__main__":
    unittest.main()
