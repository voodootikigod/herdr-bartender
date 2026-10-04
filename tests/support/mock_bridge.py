"""Scriptable mock of the Bartender NotchBar HTTP bridge.

Default behavior mirrors the original embedded MockBartenderHandler:
- ``GET /health`` -> ``{"ok": true, "port": <port>, "sessions": <n>}``
- ``POST /event`` -> 200 ``{"ok": true}``; accepted events are appended to
  ``history`` and applied to ``sessions`` (``Ended`` pops the session).
- ``return_code != 200`` answers every POST with that bare status code.
- ``delay`` sleeps before answering each POST.
- ``reject_complex_ended`` answers 400 for an ``Ended`` payload carrying keys
  beyond ``{state, agent, session_id}`` (the event is still logged in history).

On top of that, ``enqueue(status, body, delay)`` scripts the next responses in
FIFO order, and ``requests`` logs every request (including failed ones).
"""

from __future__ import annotations

import collections
import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass(frozen=True)
class ScriptedResponse:
    status: int = 200
    body: bytes = b'{"ok":true}'
    delay: float = 0.0


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False


class _Handler(BaseHTTPRequestHandler):
    bridge: "MockBridge"

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass

    def _send(self, status: int, body: bytes | None = None) -> None:
        try:
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        bridge = self.bridge
        bridge._record("GET", self.path, None, 200 if self.path == "/health" else 404)
        if self.path == "/health":
            payload = {"ok": bridge.health_ok, "port": self.server.server_port, "sessions": len(bridge.sessions)}
            self._send(200, json.dumps(payload).encode())
        else:
            self._send(404)

    def do_POST(self):
        bridge = self.bridge
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            data = None
        scripted = bridge._pop_scripted()
        delay = scripted.delay if scripted else bridge.delay
        if delay > 0:
            time.sleep(delay)

        if scripted is not None:
            bridge._record("POST", self.path, data, scripted.status)
            self._send(scripted.status, scripted.body)
            return
        if bridge.return_code != 200:
            bridge._record("POST", self.path, data, bridge.return_code)
            self._send(bridge.return_code)
            return
        if self.path != "/event":
            bridge._record("POST", self.path, data, 404)
            self._send(404)
            return
        status = bridge._apply_event(data)
        bridge._record("POST", self.path, data, status)
        if status == 400:
            self._send(400, b'{"ok":false,"error":"bad_payload"}')
        else:
            self._send(200, b'{"ok":true}')


class MockBridge:
    def __init__(self) -> None:
        self.sessions: dict = {}
        self.history: list = []
        self.requests: list = []
        self.delay = 0.0
        self.return_code = 200
        self.reject_complex_ended = False
        self.health_ok = True
        self._scripted: collections.deque = collections.deque()
        self._lock = threading.Lock()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "MockBridge":
        handler = type("BoundMockHandler", (_Handler,), {"bridge": self})
        self._server = _Server(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @property
    def port(self) -> int:
        assert self._server is not None, "bridge not started"
        return self._server.server_port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # -- scripting ---------------------------------------------------------
    def enqueue(self, status: int = 200, body: bytes | str | dict | None = None, delay: float = 0.0) -> None:
        if body is None:
            body_bytes = b'{"ok":true}' if status == 200 else b""
        elif isinstance(body, dict):
            body_bytes = json.dumps(body).encode()
        elif isinstance(body, str):
            body_bytes = body.encode()
        else:
            body_bytes = body
        with self._lock:
            self._scripted.append(ScriptedResponse(status, body_bytes, delay))

    def events_for(self, session_id: str) -> list:
        return [e for e in self.history if isinstance(e, dict) and e.get("session_id") == session_id]

    # -- internals ---------------------------------------------------------
    def _pop_scripted(self) -> ScriptedResponse | None:
        with self._lock:
            return self._scripted.popleft() if self._scripted else None

    def _record(self, method: str, path: str, body, status: int) -> None:
        with self._lock:
            self.requests.append({"method": method, "path": path, "body": body, "status": status})

    def _apply_event(self, data) -> int:
        with self._lock:
            self.history.append(data)
            if not isinstance(data, dict):
                return 200
            if self.reject_complex_ended and data.get("state") == "Ended":
                if set(data.keys()) - {"state", "agent", "session_id"}:
                    return 400
            sid = data.get("session_id")
            if data.get("state") == "Ended":
                self.sessions.pop(sid, None)
            else:
                self.sessions[sid] = data
            return 200
