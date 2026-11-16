"""Pure multi-host routing and pane-scope helpers (zero I/O, zero third-party deps).

Local pane keys stay bare canonical IDs (``w1:p1``) so single-host caches, tests,
and ``hook_guard.sh`` remain unchanged; remote hosts namespace per-pane cache tables
(``tombstones``, ``agent_exits``, ``pane_generations``) as ``<host>@<pane>``.
"""

from __future__ import annotations

from typing import Callable, Mapping, Optional, Tuple

from .intake import resolve_host
from .sanitize import sanitize_hostname, sanitized_hostname


def resolve_event_host(event_data: Mapping, pinned_host: object,
                       current: Callable[[], str] = sanitized_hostname) -> Tuple[str, bool]:
    """Return ``(effective_host, is_remote)`` for an incoming event against ``pinned_host``."""
    local_host = resolve_host(pinned_host, current)
    if isinstance(event_data, Mapping) and isinstance(event_data.get("host"), str) and event_data["host"].strip():
        ev_host = sanitize_hostname(event_data["host"])
        return ev_host, ev_host != local_host
    return local_host, False


def pane_scope_key(canonical_pane: str, host: str, local_host: str) -> str:
    """Per-host table key: bare ``canonical_pane`` locally, ``<host>@<canonical_pane>`` when remote."""
    if not canonical_pane:
        return ""
    if not host or host == local_host:
        return canonical_pane
    return f"{host}@{canonical_pane}"


def is_local_host(host: object, local_host: str) -> bool:
    """True when ``host`` is absent/empty or matches ``local_host``."""
    return host in (None, "") or host == local_host


def host_matches_session(record: Mapping, target_host: str, local_host: str) -> bool:
    """True when ``record`` belongs to ``target_host`` (records without ``host`` belong to ``local_host``)."""
    rec_host = (record.get("host") if isinstance(record, Mapping) else None) or local_host
    return rec_host == target_host


def session_id_host(session_id: object) -> Optional[str]:
    """Extract the ``<host>`` segment of a ``herdr:<host>:<pane>`` wire session ID, or None."""
    if not isinstance(session_id, str) or not session_id.startswith("herdr:"):
        return None
    parts = session_id.split(":", 2)
    return parts[1] if len(parts) == 3 and parts[1] else None


__all__ = [
    "host_matches_session",
    "is_local_host",
    "pane_scope_key",
    "resolve_event_host",
    "session_id_host",
]
