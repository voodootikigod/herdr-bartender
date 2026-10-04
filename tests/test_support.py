"""Self-tests for the test harness: sandbox isolation, shims, fake clock, mock bridge."""

import json
import os
import subprocess
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from herdr_bartender import clock, runtime
from herdr_bartender.bridge import post_bartender_event
from herdr_bartender.markers import touch_pane_failed
from herdr_bartender.paths import get_orphan_path, get_state_dir, get_vendor_hooks_dir
from herdr_bartender.process import get_bartender_pid, get_herdr_pid, get_process_start_time, own_start_time
from herdr_bartender.sanitize import get_hex_pane_id
from tests.support import SHIM_DIR, SandboxTestCase


class SandboxIsolationTests(SandboxTestCase):
    def test_paths_live_inside_sandbox(self):
        """HOME, state dir, orphan file and vendor hooks dir all resolve under the per-test tempdir."""
        for path in (Path.home(), get_state_dir(), get_orphan_path(), get_vendor_hooks_dir()):
            self.assertIn(self.tmp, Path(path).resolve().parents)

    def test_inherited_env_is_scrubbed(self):
        """No inherited HERDR_PANE_ID/HERDR_WORKSPACE_ID or proxy variables leak into tests."""
        for key in ("HERDR_PANE_ID", "HERDR_WORKSPACE_ID", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertNotIn(key, os.environ)
        self.assertTrue(os.environ["PATH"].startswith(str(SHIM_DIR)))

    def test_runtime_reset(self):
        """Runtime globals start relaxed (60s budget) and outside any critical section."""
        self.assertEqual(runtime.PROCESS_DEADLINE_SECONDS, 60.0)
        self.assertFalse(runtime.IN_CRITICAL_SECTION)
        self.assertFalse(runtime.PENDING_WATCHDOG_EXIT)


class ShimTests(SandboxTestCase):
    start_bridge = False
    default_liveness = False  # this class drives the shim process table itself

    def test_pgrep_sees_only_fake_processes(self):
        """The pgrep shim never reports real processes (e.g. a real herdr on the host)."""
        self.assertIsNone(get_herdr_pid())
        self.assertIsNone(get_bartender_pid())
        res = subprocess.run(["pgrep", "-xi", "herdr"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)

    def test_fake_herdr_and_bartender_are_discoverable(self):
        """Registered fake processes are found via pgrep -x/-xi/-f and answered by the ps shim."""
        herdr_pid = self.set_herdr_alive()
        bart_pid = self.add_fake_process("Bartender 6", pid=424242, lstart="Sat Oct  4 08:00:00 2026")
        self.assertEqual(get_herdr_pid(), herdr_pid)
        self.assertEqual(get_bartender_pid(), bart_pid)
        res = subprocess.run(["pgrep", "-f", "Herdr.app"], capture_output=True, text=True)
        self.assertEqual(res.stdout.split(), [str(herdr_pid)])
        expected = str(int(time.mktime(time.strptime("Sat Oct 4 08:00:00 2026", "%a %b %d %H:%M:%S %Y"))))
        self.assertEqual(get_process_start_time(bart_pid), expected)

    def test_ps_delegates_for_real_processes(self):
        """Unregistered PIDs (this test process) still get their genuine start time."""
        self.assertNotEqual(own_start_time(), "")
        self.assertEqual(get_process_start_time(os.getpid()), own_start_time())

    def test_osascript_shim_logs(self):
        """osascript never reaches macOS; its argv lands in the sandbox log."""
        subprocess.run(["osascript", "-e", "display notification \"x\""], check=True)
        self.assertEqual(len(self.osascript_calls()), 1)


class FakeClockTests(SandboxTestCase):
    start_bridge = False

    def test_fake_clock_drives_production_code(self):
        """Production code reads time through herdr_bartender.clock, so FakeClock controls it."""
        fake = self.use_fake_clock(start=1_800_000_000.0)
        self.assertEqual(clock.time(), 1_800_000_000.0)
        touch_pane_failed("w1:pClock")
        failed = self.state_dir / "panes" / f"{get_hex_pane_id('w1:pClock')}.failed"
        self.assertEqual(failed.read_text(), "1800000000")
        fake.sleep(5)
        self.assertEqual(clock.time(), 1_800_000_005.0)
        fake.step_back(100)
        self.assertEqual(clock.time(), 1_799_999_905.0)
        self.assertEqual(fake.sleeps, [5])


class MockBridgeTests(SandboxTestCase):
    def test_scripted_responses_and_request_log(self):
        """Scripted (status, body) responses are served FIFO and every request is logged."""
        self.bridge.enqueue(200, {"ok": False})
        self.bridge.enqueue(404)
        payload = {"state": "Working", "agent": "A", "session_id": "s1"}
        self.assertEqual(post_bartender_event(payload, bridge_url=self.mock_url), (False, True))
        self.assertEqual(post_bartender_event(payload, bridge_url=self.mock_url), (False, True))
        self.assertEqual(post_bartender_event(payload, bridge_url=self.mock_url), (True, False))
        self.assertEqual([r["status"] for r in self.bridge.requests], [200, 404, 200])
        self.assertEqual(len(self.bridge.history), 1)

    def test_health_reports_port(self):
        """/health returns ok, the bound port and the session count."""
        with urllib.request.urlopen(f"{self.mock_url}/health", timeout=2) as resp:
            body = json.loads(resp.read())
        self.assertEqual(body, {"ok": True, "port": self.bridge.port, "sessions": 0})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"{self.mock_url}/nope", timeout=2)
        ctx.exception.close()


if __name__ == "__main__":
    unittest.main()


class DefaultLivenessTests(SandboxTestCase):
    """The sandbox registers a fake Bartender and a live fake Herdr so no production test-mode bypass is needed."""

    def test_default_sandbox_has_bartender_and_herdr(self):
        """Plan §1 L135 / §8 L1085 (gap liveness-gate-bypass): liveness comes from the shim table, not UNIT_TESTING."""
        from herdr_bartender import process
        self.assertIsNotNone(get_bartender_pid())
        self.assertIs(process.herdr_liveness(), True)
        self.assertEqual(post_bartender_event({"state": "Working", "agent": "A", "session_id": "d-1"},
                                              bridge_url=self.mock_url), (True, False))

    def test_umask_is_restored_after_each_test(self):
        """Plan §8 L1089 (gap perms-umask): an in-process mark_process_start() (umask 077) does not leak."""

        class _Inner(SandboxTestCase):
            start_bridge = False

            def runTest(self):
                runtime.mark_process_start()

        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        result = unittest.TestResult()
        _Inner().run(result)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self.assertEqual(os.umask(0o022), 0o022)
