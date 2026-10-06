"""Event intake: invocation parsing, identity, admission and §2.3 field resolution.

Everything here is a pure function of its arguments except ``read_stdin_bounded``
(which only reads the stream it is given) and ``resolve_host`` (which may ask
for the current hostname). Handlers call these and keep their own cache/lease
logic.

Normative sources: plan §2.1-§2.3, §3.4 and resolutions R1 (env workspace
fallback), R2 (canonical prefix is authoritative), R3 (focus test) and R20
(stdin envelope first, then argv[1], then legacy env vars).
"""

from __future__ import annotations

import os
import re
import select
import time
from collections import Counter
from typing import Callable, Iterable, Mapping, NamedTuple, Optional, Sequence, Tuple

from . import jsonsafe
from .config import CONTAINER_ID_REGEX, PANE_ID_REGEX, SESSION_ID_REGEX, STATUS_MAP
from .sanitize import (
    normalize_pane_id,
    sanitize_agent,
    sanitize_cwd,
    sanitize_event_name,
    sanitize_title,
    sanitized_hostname,
)

STATUS_EVENT = "pane.agent_status_changed"
PANE_CLOSED = "pane.closed"
TAB_CLOSED = "tab.closed"
WORKSPACE_CLOSED = "workspace.closed"
EVENT_NAMES = frozenset({STATUS_EVENT, PANE_CLOSED, TAB_CLOSED, WORKSPACE_CLOSED})

LEGACY_EVENT_ENV = "HERDR_PLUGIN_EVENT"
LEGACY_EVENT_JSON_ENV = "HERDR_PLUGIN_EVENT_JSON"
LEGACY_CONTEXT_JSON_ENV = "HERDR_PLUGIN_CONTEXT_JSON"

STDIN_MAX_BYTES = 1 << 20
STDIN_CHUNK = 65536
WIRE_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_:-]{1,96}\Z")
HOST_RE = re.compile(r"^[a-z0-9_-]{1,32}\Z")
EVENT_NAME_VALID_RE = re.compile(r"^[A-Za-z0-9._]{1,64}\Z")
LOG_VALUE_MAX = 80


class Identity(NamedTuple):
    raw_pane: str
    canonical_pane: str
    workspace_id: str
    is_focused: bool


class Admission(NamedTuple):
    mapped_state: str
    raw_agent: str


class Fields(NamedTuple):
    title: str
    cwd: str
    tab_id: Optional[str]
    workspace_id: str


def loggable(value: object) -> str:
    """repr() bounded for log lines (never lets raw control bytes into plugin.log)."""
    return repr(value)[:LOG_VALUE_MAX]


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


# --------------------------------------------------------------------------- invocation (R20)
def read_stdin_bounded(stream, budget: float, max_bytes: int = STDIN_MAX_BYTES,
                       now: Callable[[], float] = time.monotonic) -> bytes:
    """Read ``stream`` until EOF, ``max_bytes`` or ``budget`` seconds, never blocking past the budget.

    A TTY, a missing stream or one without a usable file descriptor reads as b"".
    R89: a spent budget (slow start-up) still takes what is already buffered, without waiting, so an envelope
    Herdr wrote before we got here is never dropped.
    """
    try:
        fd = stream.fileno()
        if os.isatty(fd):
            return b""
    except (AttributeError, ValueError, OSError):  # io.UnsupportedOperation is both
        return b""
    chunks = []
    total = 0
    deadline = now() + max(0.0, budget)
    while total < max_bytes:
        remaining = max(0.0, deadline - now())
        try:
            readable, _, _ = select.select([fd], [], [], remaining)
            if not readable:
                if remaining <= 0:
                    break
                continue
            chunk = os.read(fd, min(STDIN_CHUNK, max_bytes - total))
        except (OSError, ValueError):
            break
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _parse_json_object(raw: object) -> Optional[dict]:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = jsonsafe.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def valid_event_name(raw: object) -> str:
    """``raw`` when it is a well-formed event name, else "" (never repaired into a known name)."""
    if isinstance(raw, str) and EVENT_NAME_VALID_RE.match(raw):
        return raw
    return ""


def _argv_event(argv: Sequence[str]) -> str:
    return valid_event_name(argv[0]) if argv else ""


def parse_invocation(argv: Sequence[str], stdin_bytes: bytes,
                     env: Mapping[str, str]) -> Tuple[str, Optional[dict], dict]:
    """Return ``(event_name, data, context)``; ``data`` is None when no usable payload exists.

    ``argv`` excludes the program name. Event name precedence: envelope ``event``,
    argv[1] (when not a flag), then legacy ``HERDR_PLUGIN_EVENT``.
    """
    envelope = _parse_json_object(stdin_bytes)
    if envelope is not None:
        name = valid_event_name(envelope.get("event")) or _argv_event(argv) \
            or valid_event_name(env.get(LEGACY_EVENT_ENV))
        return name, _as_dict(envelope.get("data")), _as_dict(envelope.get("context"))
    name = _argv_event(argv) or valid_event_name(env.get(LEGACY_EVENT_ENV))
    legacy = _parse_json_object(env.get(LEGACY_EVENT_JSON_ENV))
    context = _parse_json_object(env.get(LEGACY_CONTEXT_JSON_ENV)) or {}
    if legacy is None:
        return name, None, context
    return name, _as_dict(legacy.get("data")), context


# --------------------------------------------------------------------------- identity (R1/R2/R3)
def container_id(data: Mapping, key: str) -> Optional[str]:
    """A valid container id from event data only (never from focused context)."""
    value = data.get(key)
    if isinstance(value, str) and CONTAINER_ID_REGEX.match(value):
        return value
    return None


def resolve_identity(data: Mapping, context: Mapping,
                     env: Optional[Mapping[str, str]] = None) -> Tuple[Optional[Identity], Tuple[str, ...]]:
    """Canonical pane identity for an event, plus log notes. None means: drop the event."""
    data, context = _as_dict(data), _as_dict(context)
    raw = data.get("pane_id")
    if not isinstance(raw, str) or not raw:
        return None, (f"Rejecting event without a valid pane_id: {loggable(raw)}",)
    notes = []
    event_ws = container_id(data, "workspace_id")
    if data.get("workspace_id") not in (None, "") and event_ws is None:
        notes.append(f"Ignoring invalid event workspace_id {loggable(data.get('workspace_id'))}")
    canonical = normalize_pane_id(raw, event_ws, env)
    if not canonical or not PANE_ID_REGEX.match(canonical):
        notes.append(f"Rejecting invalid canonical pane ID {loggable(canonical)} (raw {loggable(raw)})")
        return None, tuple(notes)
    prefix = canonical.split(":", 1)[0]
    if event_ws and event_ws != prefix:
        notes.append(f"workspace_id mismatch: event {loggable(event_ws)} vs pane prefix {loggable(prefix)}; using prefix")
    focused_id = context.get("focused_pane_id")
    is_focused = isinstance(focused_id, str) and bool(focused_id) and focused_id in (canonical, raw)
    return Identity(raw, canonical, prefix, is_focused), tuple(notes)


# --------------------------------------------------------------------------- admission (§2.2)
def classify_status(agent_status: object) -> str:
    if isinstance(agent_status, str) and agent_status in STATUS_MAP:
        return "valid"
    if agent_status == "unknown":
        return "unknown"
    return "unrecognized"


def _focused_agent(context: Mapping, identity: Identity) -> str:
    return sanitize_agent(context.get("focused_pane_agent")) if identity.is_focused else ""


def _admit_idle(data: Mapping, cached: Mapping, live: bool) -> Optional[Admission]:
    cached_agent = sanitize_agent(cached.get("raw_agent"))
    if "agent" in data:
        event_agent = sanitize_agent(data.get("agent"))
        if event_agent:
            return Admission("Idle", event_agent)                # Case A
        return Admission("Ended", cached_agent) if live else None  # Case B
    if live and cached_agent:
        return Admission("Idle", cached_agent)                   # Case C
    return None


def is_agent_exit_signal(data: Mapping) -> bool:
    """§3.4 L249 / Case B: ``idle`` with an explicitly empty agent, i.e. a potential agent exit to Ended.

    Pure and cache-free, so it can classify spooled envelopes (close protection,
    salvage) without knowing whether the pane had a live session.
    """
    data = _as_dict(data)
    return data.get("agent_status") == "idle" and "agent" in data and not sanitize_agent(data.get("agent"))


def admit_status(agent_status: str, data: Mapping, context: Mapping, identity: Identity,
                 cached: Mapping) -> Optional[Admission]:
    """§2.2 admission + Cases A/B/C. ``cached`` is the existing session record ({} if none).

    Returns the mapped Bartender state and the (sanitized) agent to display, or
    None when the event must be discarded. The cached agent is used for display
    only after admission, so it can never resurrect an Ended session.
    """
    data, context, cached = _as_dict(data), _as_dict(context), _as_dict(cached)
    live = bool(cached) and cached.get("desired_state") != "Ended"
    if agent_status == "idle":
        return _admit_idle(data, cached, live)
    mapped = STATUS_MAP.get(agent_status) if isinstance(agent_status, str) else None
    if not mapped:
        return None
    positive = sanitize_agent(data.get("agent")) or _focused_agent(context, identity)
    if not positive and not live:
        return None
    return Admission(mapped, positive or sanitize_agent(cached.get("raw_agent")))


# --------------------------------------------------------------------------- fields (§2.3)
def _first_nonempty(candidates: Iterable[object], clean: Callable[[object], str]) -> str:
    for candidate in candidates:
        value = clean(candidate)
        if value:
            return value
    return ""


def resolve_fields(data: Mapping, context: Mapping, identity: Identity, cached: Mapping) -> Fields:
    data, context, cached = _as_dict(data), _as_dict(context), _as_dict(cached)
    focused = identity.is_focused
    title = _first_nonempty(
        (data.get("title"), cached.get("title"), context.get("workspace_label") if focused else None),
        sanitize_title,
    ) or sanitize_title(f"Pane {identity.canonical_pane}")
    cwd = _first_nonempty(
        (data.get("cwd"), cached.get("cwd"),
         context.get("focused_pane_cwd") if focused else None,
         context.get("workspace_cwd") if focused else None),
        sanitize_cwd,
    )
    tab_id = container_id(data, "tab_id") or container_id(cached, "tab_id") \
        or (container_id(context, "tab_id") if focused else None)
    return Fields(title, cwd, tab_id, identity.workspace_id)


# --------------------------------------------------------------------------- host / session id
def resolve_host(pinned: object, current: Callable[[], str] = sanitized_hostname) -> str:
    """The cache-pinned host when it is valid, otherwise the current sanitized hostname."""
    if isinstance(pinned, str) and HOST_RE.match(pinned):
        return pinned
    return current()


def build_session_id(host: str, canonical_pane: str) -> str:
    """``herdr:{host}:{pane}`` or "" when it would violate the 96-char / SESSION_ID_REGEX bounds."""
    sid = f"herdr:{host}:{canonical_pane}"
    if WIRE_SESSION_ID_RE.match(sid) and SESSION_ID_REGEX.match(sid):
        return sid
    return ""


def pick_salvage_host(session_ids: Iterable[object], fallback: str) -> str:
    """Most common valid host component among salvaged session ids (plan L273), else ``fallback``."""
    hosts = Counter()
    for sid in session_ids:
        if isinstance(sid, str) and SESSION_ID_REGEX.match(sid):
            host = sid.split(":", 2)[1]
            if HOST_RE.match(host):
                hosts[host] += 1
    return hosts.most_common(1)[0][0] if hosts else fallback


# --------------------------------------------------------------------------- container matching
def session_matches_pane(sid: str, info: Mapping, canonical_pane: str, host: str) -> bool:
    """Exact match only: cached canonical pane, or the exact wire session id (never a suffix)."""
    if _as_dict(info).get("pane_id") == canonical_pane:
        return True
    return sid == f"herdr:{host}:{canonical_pane}"


def _qualify_tab(tab: object, workspace_id: object) -> Optional[str]:
    if not isinstance(tab, str) or not tab:
        return None
    if ":" in tab:
        return tab
    if isinstance(workspace_id, str) and workspace_id:
        return f"{workspace_id}:{tab}"
    return None


def session_matches_tab(info: Mapping, closed_tab: str, closed_workspace: Optional[str]) -> bool:
    """Equality of workspace-qualified tab ids; an unqualifiable side never matches."""
    info = _as_dict(info)
    cached = _qualify_tab(info.get("tab_id"), info.get("workspace_id"))
    closed = _qualify_tab(closed_tab, closed_workspace)
    return cached is not None and cached == closed


def session_matches_workspace(info: Mapping, workspace_id: str) -> bool:
    info = _as_dict(info)
    if info.get("workspace_id") == workspace_id:
        return True
    pane = info.get("pane_id")
    return isinstance(pane, str) and ":" in pane and pane.split(":", 1)[0] == workspace_id


def build_close_payload(sid: str, info: Mapping, event_name: str, seq: int) -> dict:
    """Full Ended payload for close events (the 3-field form is only the bridge retry fallback)."""
    info = _as_dict(info)
    return {
        "state": "Ended",
        "agent": info.get("agent") or "Herdr",
        "session_id": sid,
        "title": info.get("title") or "",
        "cwd": info.get("cwd") or "",
        "terminal": "Herdr",
        "event": sanitize_event_name(event_name),
        "seq": seq,
    }
