"""Terminal-control stripping, field sanitization and canonical pane-ID normalization.

Plan §2.3: title/agent/cwd are stripped of ANSI CSI, OSC, DCS and C0/C1 control
characters (one shared helper) and bounded (title 120, agent 64, cwd 256); the
``event`` field is reduced to ``[A-Za-z0-9._]`` (64) -- the underscore is kept
because the real event name ``pane.agent_status_changed`` contains it. Hostnames follow
``re.sub(r'[^a-z0-9_-]', '', host.split('.')[0].lower())[:32] or "local"``.
"""

from __future__ import annotations

import os
import re
import socket
from typing import Mapping, Optional

from .config import AGENT_NAME_OVERRIDES

TITLE_MAX = 120
AGENT_MAX = 64
CWD_MAX = 256
EVENT_NAME_MAX = 64
HOST_MAX = 32
AGENT_SUFFIX = " (Herdr)"
DEFAULT_AGENT_NAME = "Herdr"
DEFAULT_HOST = "local"

# ECMA-48: 7-bit (ESC [) or 8-bit (0x9B) introducer, parameter bytes 0x30-0x3F,
# intermediate bytes 0x20-0x2F, final byte 0x40-0x7E.
CSI_RE = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
# OSC (ESC ] / 0x9D) ends at BEL, ST (ESC \) or 0x9C; an unterminated tail runs to end of string.
OSC_RE = re.compile(r"(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c|$)")
# DCS (ESC P / 0x90), plus SOS/PM/APC (ESC X/^/_ and 0x98/0x9E/0x9F), end at ST or end of string.
DCS_RE = re.compile(r"(?:\x1b[PX^_]|[\x90\x98\x9e\x9f])[^\x1b\x9c]*(?:\x1b\\|\x9c|$)")
# Any other two-byte escape (e.g. ESC c, ESC 7).
ESC_RE = re.compile(r"\x1b[@-Z\\-~]?")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
EVENT_NAME_RE = re.compile(r"[^A-Za-z0-9._]")
HOST_STRIP_RE = re.compile(r"[^a-z0-9_-]")


def _as_text(raw: object) -> str:
    if raw is None:
        return ""
    return raw if isinstance(raw, str) else str(raw)


def strip_terminal_controls(raw: object) -> str:
    """Remove CSI, OSC, DCS/SOS/PM/APC, stray escapes and every C0/C1 control."""
    text = _as_text(raw)
    for pattern in (OSC_RE, DCS_RE, CSI_RE, ESC_RE, CONTROL_RE):
        text = pattern.sub("", text)
    return text


def sanitize_title(raw: object) -> str:
    return strip_terminal_controls(raw).strip()[:TITLE_MAX]


def sanitize_agent(raw: object) -> str:
    """Control-stripped, whitespace-trimmed agent identifier (<= 64 chars); '' when absent."""
    return strip_terminal_controls(raw).strip()[:AGENT_MAX]


def sanitize_cwd(raw: object) -> str:
    return strip_terminal_controls(raw)[:CWD_MAX]


def sanitize_event_name(raw: object) -> str:
    return EVENT_NAME_RE.sub("", _as_text(raw))[:EVENT_NAME_MAX]


def get_hex_pane_id(raw_id: Optional[str]) -> str:
    if not raw_id:
        return ""
    return str(raw_id).encode("utf-8").hex()


def format_agent_name(raw_agent: object) -> str:
    """Display name: AGENT_NAME_OVERRIDES / capitalized, suffixed ' (Herdr)', total <= 64 chars."""
    clean = sanitize_agent(raw_agent).lower()
    if not clean:
        return DEFAULT_AGENT_NAME
    pretty = AGENT_NAME_OVERRIDES.get(clean, clean.capitalize())
    return f"{pretty[:AGENT_MAX - len(AGENT_SUFFIX)]}{AGENT_SUFFIX}"


def normalize_pane_id(raw_pane_id: object, workspace_id: Optional[str],
                      env: Optional[Mapping[str, str]] = None) -> str:
    """R1: colon-qualified IDs pass through; otherwise ws = workspace_id or $HERDR_WORKSPACE_ID.

    Returns "" when the pane is missing or no workspace is available (never "default").
    """
    if not isinstance(raw_pane_id, str) or not raw_pane_id:
        return ""
    if ":" in raw_pane_id:
        return raw_pane_id
    environ = os.environ if env is None else env
    ws = workspace_id or environ.get("HERDR_WORKSPACE_ID") or ""
    return f"{ws}:{raw_pane_id}" if ws else ""


def sanitize_hostname(raw_host: object) -> str:
    raw = _as_text(raw_host).split(".")[0].lower()
    return HOST_STRIP_RE.sub("", raw)[:HOST_MAX] or DEFAULT_HOST


def sanitized_hostname() -> str:
    try:
        raw = socket.gethostname()
    except OSError:
        raw = ""
    return sanitize_hostname(raw)
