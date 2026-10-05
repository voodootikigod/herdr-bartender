"""npx adversarial-review gate round 23: a total deadline for bridge exchanges, and hook repair that never
overwrites a concurrent vendor update (R71)."""

import socket
import threading
import time
import unittest
import urllib.request
from unittest import mock

from herdr_bartender import bridge, hooks_fs
from herdr_bartender.boundedio import read_regular_file as real_read
from tests.support import SandboxTestCase


class _TricklingServer:
    """Accepts one connection, sends a status line, then drips header bytes forever (well inside any timeout)."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            try:
                conn.recv(65536)
                conn.sendall(b"HTTP/1.1 200 OK\r\nX-Drip: ")
                while not self.stop.is_set():
                    conn.sendall(b"a")
                    time.sleep(0.03)
            except OSError:
                return

    def close(self):
        self.stop.set()
        self.sock.close()


class BridgeTotalDeadlineTests(SandboxTestCase):
    def test_trickling_listener_cannot_hold_a_request_open(self):
        """R71: per-operation timeouts alone let a trickle keep a reader blocked; the exchange has one deadline."""
        server = _TricklingServer()
        self.addCleanup(server.close)
        req = urllib.request.Request(f"http://127.0.0.1:{server.port}/event", data=b"{}", method="POST")
        box = {}
        started = time.monotonic()
        worker = threading.Thread(target=lambda: box.update(result=bridge._perform(req, timeout=0.2)), daemon=True)
        worker.start()
        worker.join(3.0)
        elapsed = time.monotonic() - started
        if worker.is_alive():
            server.close()   # releases the stuck reader
            self.fail("the trickle held the request open past any deadline")
        status, body, error = box["result"]
        self.assertIsNone(status)
        self.assertIsNotNone(error)
        self.assertLess(elapsed, 1.5, "bounded by 2 x the request timeout, not by the trickle")


class HookSwapRaceTests(SandboxTestCase):
    def test_vendor_update_after_the_check_is_kept(self):
        """R71: a vendor update landing between the pre-check and the replace is not overwritten."""
        hook = self.tmp / "claude-event-hook.sh"
        original = b"#!/bin/bash\necho vendor v1\n"
        update = b"#!/bin/bash\necho vendor v2\n"
        hook.write_bytes(original)
        calls = []

        def read_then_update(path, *args, **kwargs):
            data = real_read(path, *args, **kwargs)
            if not calls and path == hook:
                calls.append(path)
                hook.write_bytes(update)   # the vendor's updater lands right after our check
            return data

        with mock.patch.object(hooks_fs, "read_regular_file", side_effect=read_then_update):
            with self.assertRaises(hooks_fs.HookWriteError):
                hooks_fs.atomic_replace_hook(hook, b"#!/bin/bash\necho patched\n", 0o755, expected=original)
        self.assertEqual(hook.read_bytes(), update, "the vendor's version wins")
        self.assertEqual(list(self.tmp.glob("claude-event-hook.sh.tmp.*")), [])


if __name__ == "__main__":
    unittest.main()
