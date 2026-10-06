"""Security boundaries (Plan §8): no redirects, no proxies, literal loopback, liveness gate, file modes."""

from __future__ import annotations

import json
import os
import stat
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from herdr_bartender import bridge, paths, runtime
from herdr_bartender.bridge import check_bridge_health, deliver_event, post_bartender_event
from herdr_bartender.log import log_debug
from tests.support import SandboxTestCase

FAKE_BARTENDER_PID = 4_000_002
PAYLOAD = {"state": "Working", "agent": "A", "session_id": "sec-1"}


class _RedirectingServer:
    """Loopback server that answers every POST /event and GET /health with a 302."""

    def __init__(self, status: int = 302) -> None:
        self.paths: list = []
        server_self = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: D401 - silence stdlib logging
                pass

            def _answer(self):
                server_self.paths.append((self.command, self.path))
                if self.path in ("/event", "/health"):
                    self.send_response(status)
                    self.send_header("Location", "/other")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._answer()

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0) or 0)
                self.rfile.read(length)
                self._answer()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                                        daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def start(self) -> "_RedirectingServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class TransportSecurityTests(SandboxTestCase):
    default_liveness = False  # this class drives the shim process table itself
    def setUp(self):
        super().setUp()
        self.add_fake_process("Bartender 6", pid=FAKE_BARTENDER_PID, lstart="Sat Oct  4 07:00:00 2026")

    def _redirector(self, status: int = 302) -> _RedirectingServer:
        server = _RedirectingServer(status).start()
        self.addCleanup(server.stop)
        return server

    def test_redirects_are_not_followed(self):
        """Plan §3.3 3xx row / §8 L1087 (gap redirect-followed): 301/302/303/307/308 are unexpected_redirect, never followed."""
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                server = self._redirector(status)
                result = deliver_event(PAYLOAD, bridge_url=server.url)
                self.assertEqual((result.outcome, result.error), ("non_retryable", "unexpected_redirect"))
                self.assertEqual(server.paths, [("POST", "/event")], "the redirect target must not be requested")

    def test_health_check_does_not_follow_redirects(self):
        """Plan §8 L1087 (gap redirect-followed): /health redirects are failures, not followed."""
        server = self._redirector(302)
        self.assertIsNone(check_bridge_health(bridge_url=server.url))
        self.assertEqual(server.paths, [("GET", "/health")])

    def test_proxy_environment_is_ignored(self):
        """Plan §8 L1085/L1087 (gap proxy-leak): HTTP(S)_PROXY never diverts loopback bridge traffic.

        The opener is rebuilt after the proxy variables are set, so a build that consulted the
        environment (a default ProxyHandler) would route through the dead proxy and fail.
        """
        dead_proxy = "http://127.0.0.1:9"
        for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            os.environ[key] = dead_proxy
        os.environ.pop("NO_PROXY", None)
        os.environ.pop("no_proxy", None)
        self.assertTrue(urllib.request.getproxies(), "precondition: the environment advertises proxies")
        opener = bridge.build_bridge_opener()
        for built in (opener, bridge._OPENER):
            # An empty ProxyHandler registers no *_open methods, so none may appear at all;
            # build_opener()'s default one would read the proxies above and show up here.
            self.assertEqual([h for h in built.handlers if isinstance(h, urllib.request.ProxyHandler)], [])
        patcher = mock.patch.object(bridge, "_OPENER", opener)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertEqual(post_bartender_event(PAYLOAD, bridge_url=self.mock_url), (True, False))
        self.assertEqual(self.bridge.events_for("sec-1"), [PAYLOAD])
        self.assertTrue(check_bridge_health(bridge_url=self.mock_url)["ok"])

    def test_non_loopback_bridge_url_is_refused(self):
        """Plan §8 L1087 literal 127.0.0.1 (gap proxy-leak): other hosts/schemes are refused without I/O."""
        for url in ("http://localhost:7823", "http://10.0.0.1:7823", "https://127.0.0.1:7823",
                    "http://127.0.0.1.evil.example:7823", "http://user@127.0.0.1:7823", "file:///etc/passwd"):
            with self.subTest(url=url):
                result = deliver_event(PAYLOAD, bridge_url=url)
                self.assertEqual((result.outcome, result.error), ("non_retryable", "invalid_bridge_url"))
                self.assertIsNone(check_bridge_health(bridge_url=url))


class LivenessGateTests(SandboxTestCase):
    default_liveness = False  # this class drives the shim process table itself

    def test_gate_refuses_event_without_bartender_even_with_explicit_url(self):
        """Plan §1 L135 / §8 L1085 (gap liveness-gate-bypass): no Bartender => (False, False), no HTTP hit."""
        for url in (None, self.mock_url):
            with self.subTest(bridge_url=url):
                result = deliver_event(PAYLOAD, bridge_url=url)
                self.assertEqual((result.outcome, result.error), ("retryable", "bartender_not_running"))
                self.assertEqual(post_bartender_event(PAYLOAD, bridge_url=url), (False, False))
        self.assertEqual(self.bridge.requests, [])

    def test_gate_refuses_health_without_bartender_even_with_explicit_url(self):
        """Plan §8 L1085 (gap liveness-gate-bypass): health checks are gated too."""
        self.assertIsNone(check_bridge_health())
        self.assertIsNone(check_bridge_health(bridge_url=self.mock_url))
        self.assertEqual(self.bridge.requests, [])

    def test_gate_opens_when_fake_bartender_runs(self):
        """Plan §8 L1085 (gap liveness-gate-bypass): the shims' fake Bartender opens the gate, default URL included."""
        self.add_fake_process("Bartender 6", pid=FAKE_BARTENDER_PID, lstart="Sat Oct  4 07:00:00 2026")
        self.assertEqual(post_bartender_event(PAYLOAD), (True, False))
        self.assertTrue(check_bridge_health()["ok"])
        self.assertEqual([r["path"] for r in self.bridge.requests], ["/event", "/health"])

    def test_gate_unknown_when_pgrep_fails(self):
        """Plan §6.1 (gaps liveness-gate-bypass, subprocess-no-timeout): an unknown probe defers, never sends."""
        self.add_fake_process("Bartender 6", pid=FAKE_BARTENDER_PID, lstart="Sat Oct  4 07:00:00 2026")
        (self.sandbox / "pgrep.fail").write_text("")
        self.assertEqual(post_bartender_event(PAYLOAD), (False, False))
        self.assertEqual(self.bridge.requests, [])


class FilePermissionTests(SandboxTestCase):
    start_bridge = False

    def _mode(self, path) -> int:
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_state_dir_created_0700(self):
        """Plan §8 L1089 (gap perms-umask): the state directory is created rwx------."""
        self.assertEqual(self._mode(self.state_dir), 0o700)

    def test_existing_loose_state_dir_is_tightened(self):
        """Plan §8 L1089 (gap perms-umask): a pre-existing 0755 state dir is chmod-ed to 0700."""
        os.chmod(self.state_dir, 0o755)
        self.assertEqual(self._mode(paths.get_state_dir()), 0o700)

    def test_private_subdir_helper(self):
        """Plan §8 L1089 (gap perms-umask): panes/, spool/ etc. are created (or tightened to) 0700."""
        panes = paths.ensure_private_dir(self.state_dir / "panes")
        self.assertEqual(self._mode(panes), 0o700)
        os.chmod(panes, 0o775)
        paths.ensure_private_dir(panes)
        self.assertEqual(self._mode(panes), 0o700)

    def test_marker_writers_tighten_loose_panes_dir(self):
        """Plan §8 L1089 (gap perms-umask): touch_pane_marker/touch_pane_failed tighten an existing 0755 panes/."""
        from herdr_bartender import markers
        panes = self.state_dir / "panes"
        for writer in (markers.touch_pane_marker, markers.touch_pane_failed):
            with self.subTest(writer=writer.__name__):
                panes.mkdir(exist_ok=True)
                os.chmod(panes, 0o755)
                writer("w1:p1")
                self.assertEqual(self._mode(panes), 0o700)

    def test_log_file_is_0600_even_when_created_loose(self):
        """Plan §8 L1089 (gap perms-umask): plugin.log is rw------- regardless of the inherited umask."""
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        log_debug("first line")
        log_file = self.state_dir / "plugin.log"
        self.assertEqual(self._mode(log_file), 0o600)
        os.chmod(log_file, 0o644)
        log_debug("second line")
        self.assertEqual(self._mode(log_file), 0o600)
        self.assertIn("second line", log_file.read_text())

    def test_private_umask_helper(self):
        """Plan §8 L1089 (gap perms-umask): apply_private_umask() sets 077 and returns the previous mask."""
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        self.assertEqual(paths.apply_private_umask(), 0o022)
        probe = self.tmp / "probe"
        probe.write_text("x")
        self.assertEqual(self._mode(probe), 0o600)
        self.assertEqual(os.umask(0o022), 0o077)

    def test_log_rotation_keeps_private_mode(self):
        """Plan §5.1 item 12 log hygiene + §8 (gap perms-umask): the rotated log stays 0600."""
        log_file = self.state_dir / "plugin.log"
        log_debug("seed")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write("x" * (1_048_576 + 10))
        log_debug("after rotation")
        self.assertEqual(self._mode(self.state_dir / "plugin.log.1"), 0o600)
        self.assertEqual(self._mode(log_file), 0o600)
        self.assertIn("after rotation", log_file.read_text())


STATUS_ENVELOPE = {
    "event": "pane.agent_status_changed",
    "data": {"pane_id": "w1:pPerm", "workspace_id": "w1", "tab_id": "w1:t1", "agent": "claude",
             "agent_status": "working", "title": "perm probe", "timestamp": 1727998410.12},
    "context": {"focused_pane_id": "w1:pPerm", "workspace_id": "w1", "tab_id": "w1:t1"},
}


class ProcessUmaskTests(SandboxTestCase):
    """Plan §8 L1089: the plugin process creates nothing group/world accessible."""

    def _modes_under(self, root):
        found = {}
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames + filenames:
                path = os.path.join(dirpath, name)
                found[os.path.relpath(path, root)] = (os.path.isdir(path), stat.S_IMODE(os.lstat(path).st_mode))
        return found

    def test_process_start_applies_private_umask(self):
        """Plan §8 L1089 (gap perms-umask): runtime.mark_process_start(), the first statement of
        cli.main(), sets umask 077 before any state is touched."""
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        runtime.mark_process_start()
        self.assertEqual(os.umask(0o022), 0o077)

    def test_event_under_loose_umask_creates_private_files(self):
        """Plan §8 L1089 (gap perms-umask): an event run under umask 022 leaves every state file 0600
        and every state directory 0700."""
        self.add_fake_process("Bartender 6", pid=FAKE_BARTENDER_PID, lstart="Sat Oct  4 07:00:00 2026")
        old = os.umask(0o022)
        try:
            res = self.run_cli("pane.agent_status_changed", input=json.dumps(STATUS_ENVELOPE).encode())
        finally:
            os.umask(old)
        self.assertEqual(res.returncode, 0, res.stderr)
        modes = self._modes_under(self.state_dir)
        self.assertTrue(any(not is_dir for is_dir, _ in modes.values()), "the event created state files")
        loose = {rel: oct(mode) for rel, (is_dir, mode) in modes.items()
                 if mode != (0o700 if is_dir else 0o600)}
        self.assertEqual(loose, {})


if __name__ == "__main__":
    unittest.main()
