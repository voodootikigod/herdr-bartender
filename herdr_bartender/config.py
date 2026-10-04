"""Static configuration: bridge defaults, agent naming, ID validation regexes."""

from __future__ import annotations

import os
import re
import socket


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7823

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

CSI_REGEX = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
OSC_REGEX = re.compile(r'\x1b\][^\x07\x1b]*(\x07|\x1b\\)')
DCS_REGEX = re.compile(r'\x1bP[^\x1b]*\x1b\\')
CONTROL_REGEX = re.compile(r'[\x00-\x1f\x7f-\x9f]')
PANE_ID_REGEX = re.compile(r'^[a-zA-Z0-9_:-]{1,48}$')
CONTAINER_ID_REGEX = re.compile(r'^[a-zA-Z0-9_:-]{1,48}$')
SESSION_ID_REGEX = re.compile(r'^herdr:[a-zA-Z0-9_-]{1,32}:[a-zA-Z0-9_:-]{1,48}$')


def get_sanitized_hostname() -> str:
    raw = socket.gethostname().split('.')[0].lower()
    clean = re.sub(r'[^a-z0-9_-]', '', raw)
    return clean or "local"


def get_bridge_url() -> str:
    port = os.environ.get("NOTCHBAR_AGENTS_PORT") or str(DEFAULT_PORT)
    return f"http://127.0.0.1:{port}"
