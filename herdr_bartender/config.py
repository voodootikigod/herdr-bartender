"""Static configuration: bridge defaults, agent naming, ID validation regexes."""

from __future__ import annotations

import os
import re
import socket

from .log import log_debug, log_warning


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7823
PORT_ENV = "NOTCHBAR_AGENTS_PORT"
MIN_PORT = 1024
MAX_PORT = 65535
_PORT_REGEX = re.compile(r'^[0-9]{1,5}$')

AGENT_NAME_OVERRIDES = {
    "claude": "Claude",
    "codex": "Codex",
    "kilo": "Kilo",
    "grok": "Grok",
    "cursor": "Cursor",
    "copilot": "Copilot",
    "kimi": "Kimi",
    "opencode": "OpenCode",
}

STATUS_MAP = {
    "blocked": "Waiting",   # Awaiting user input / permission -> NotchBar highlight
    "working": "Working",   # Turn or tool running
    "done": "Done",         # Completed
    "idle": "Idle",         # Idle at prompt
}

PANE_ID_REGEX = re.compile(r'^[a-zA-Z0-9_:-]{1,48}$')
CONTAINER_ID_REGEX = re.compile(r'^[a-zA-Z0-9_:-]{1,48}$')
SESSION_ID_REGEX = re.compile(r'^herdr:[a-zA-Z0-9_-]{1,32}:[a-zA-Z0-9_:-]{1,48}$')
# Plan §1 L38: the hook guard records only vendor session UUIDs matching this pattern.
VENDOR_UUID_REGEX = re.compile(r'^[a-zA-Z0-9_-]{16,64}$')


HOST_MAX_LEN = 32


def get_sanitized_hostname() -> str:
    """Plan §2.3 session_id row: lowercase short host, [a-z0-9_-] only, at most 32 chars, else 'local'."""
    try:
        raw = socket.gethostname()
    except OSError as e:
        log_debug(f"gethostname failed ({e}); using 'local'")
        raw = ""
    clean = re.sub(r'[^a-z0-9_-]', '', raw.split('.')[0].lower())
    return clean[:HOST_MAX_LEN] or "local"


def parse_port(raw: str | None) -> int | None:
    """Strictly parse a bridge port: a 1-5 digit integer in 1024-65535, else None."""
    if raw is None or not _PORT_REGEX.match(raw):
        return None
    port = int(raw)
    return port if MIN_PORT <= port <= MAX_PORT else None


def get_bridge_port() -> int:
    """Validated NOTCHBAR_AGENTS_PORT (Plan §8): unset/empty -> 7823; invalid -> 7823 plus a warning."""
    raw = os.environ.get(PORT_ENV)
    if not raw:
        return DEFAULT_PORT
    port = parse_port(raw)
    if port is None:
        log_warning(f"invalid {PORT_ENV}={raw!r} (need integer {MIN_PORT}-{MAX_PORT}); using {DEFAULT_PORT}")
        return DEFAULT_PORT
    return port


def get_bridge_url() -> str:
    """Literal-loopback bridge URL (Plan §8 L1087: no hostname resolution)."""
    return f"http://{DEFAULT_HOST}:{get_bridge_port()}"
