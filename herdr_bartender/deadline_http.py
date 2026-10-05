"""R71: one wall-clock deadline for a whole bridge request (connect, status line, headers and body).

urllib's ``timeout`` bounds each socket operation, not the exchange: a listener that trickles a byte just
inside every timeout could hold a reader forever. Here the connection's socket re-arms its timeout before
every receive to what is left of a single deadline fixed when the request starts, and fails with
``socket.timeout`` once it has passed.
"""

from __future__ import annotations

import functools
import http.client
import socket
import time
import urllib.request

# The whole exchange may take this many times the per-operation timeout (a connect, then one or more reads).
TOTAL_DEADLINE_FACTOR = 2.0


class _DeadlineSocket(socket.socket):
    """A connected socket whose every receive is bounded by ``deadline`` (``time.monotonic()``)."""

    deadline = 0.0
    per_call = None

    def _arm(self) -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("bridge response deadline exceeded")
        self.settimeout(remaining if self.per_call is None else min(self.per_call, remaining))

    def recv(self, *args, **kwargs):
        self._arm()
        return super().recv(*args, **kwargs)

    def recv_into(self, *args, **kwargs):
        self._arm()
        return super().recv_into(*args, **kwargs)


class DeadlineHTTPConnection(http.client.HTTPConnection):
    """``HTTPConnection`` whose socket enforces one total deadline, fixed at construction."""

    def __init__(self, *args, total_seconds: float = 1.0, **kwargs):
        super().__init__(*args, **kwargs)
        self._deadline = time.monotonic() + max(0.0, total_seconds)

    def connect(self) -> None:
        super().connect()
        raw = self.sock
        sock = _DeadlineSocket(raw.family, raw.type, raw.proto, fileno=raw.detach())
        sock.deadline = self._deadline
        sock.per_call = self.timeout if isinstance(self.timeout, (int, float)) else None
        self.sock = sock


class DeadlineHTTPHandler(urllib.request.HTTPHandler):
    """Replaces urllib's HTTPHandler: every request gets a total deadline of factor x its timeout."""

    def http_open(self, req):
        timeout = req.timeout if isinstance(req.timeout, (int, float)) else 1.0
        connection = functools.partial(DeadlineHTTPConnection, total_seconds=timeout * TOTAL_DEADLINE_FACTOR)
        return self.do_open(connection, req)
