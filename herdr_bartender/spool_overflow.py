"""R83: at the close-spool ceiling, a close that can matter evicts one that cannot - no relevant close is lost.

A spooled close only matters if, when replayed, it can end something: a session cached now, or one a pending
status envelope may still admit. Those are bounded (the 256-session cache plus the 100-envelope status spool),
far below ``CLOSE_HARD_CAP``. So when the ceiling is reached, a relevant new close evicts the oldest spooled
close whose target matches nothing; an irrelevant new close is the one refused (its replay would be a no-op).

Read-only and lock-free with respect to the cache (a bounded snapshot read); called under the spool lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, Iterable, Mapping, Optional

from . import jsonsafe
from .boundedio import CACHE_MAX_BYTES, read_regular_file
from .envelopes import read_json
from .intake import PANE_CLOSED, TAB_CLOSED, WORKSPACE_CLOSED

CACHE_FILE_NAME = "active-sessions.json"


@dataclass(frozen=True)
class Targets:
    panes: FrozenSet[str]
    tabs: FrozenSet[str]
    workspaces: FrozenSet[str]


def _canonical_pane(data: Mapping) -> Optional[str]:
    pane, ws = data.get("pane_id"), data.get("workspace_id")
    if not isinstance(pane, str) or not pane:
        return None
    if ":" in pane:
        return pane
    return f"{ws}:{pane}" if isinstance(ws, str) and ws else None


def _cached_sessions(state_dir: Path) -> Optional[Iterable[Mapping]]:
    try:
        data = jsonsafe.loads(read_regular_file(Path(state_dir) / CACHE_FILE_NAME, CACHE_MAX_BYTES))
    except FileNotFoundError:
        return ()
    except (OSError, ValueError):
        return None   # unreadable: everything must be treated as relevant
    sessions = data.get("sessions") if isinstance(data, dict) else None
    return [s for s in sessions.values() if isinstance(s, dict)] if isinstance(sessions, dict) else None


def live_targets(state_dir: Path, status_envelopes: Iterable[Path]) -> Optional[Targets]:
    """Everything a close could still end; None when the cache cannot be read (then nothing is evictable)."""
    sessions = _cached_sessions(state_dir)
    if sessions is None:
        return None
    panes, tabs, workspaces = set(), set(), set()
    for record in sessions:
        pane = record.get("pane_id")
        if isinstance(pane, str):
            panes.add(pane)
            if ":" in pane:
                workspaces.add(pane.split(":", 1)[0])
        for key, bucket in (("tab_id", tabs), ("workspace_id", workspaces)):
            if isinstance(record.get(key), str):
                bucket.add(record[key])
    for path in status_envelopes:
        try:
            env = read_json(path)
        except (OSError, ValueError):
            continue
        data = env.get("event_data") if isinstance(env, dict) else None
        if isinstance(data, dict):
            pane = _canonical_pane(data)
            if pane:
                panes.add(pane)
                workspaces.add(pane.split(":", 1)[0])
            for key, bucket in (("tab_id", tabs), ("workspace_id", workspaces)):
                if isinstance(data.get(key), str):
                    bucket.add(data[key])
    return Targets(frozenset(panes), frozenset(tabs), frozenset(workspaces))


def close_is_relevant(event_name: str, event_data: Mapping, targets: Optional[Targets]) -> bool:
    """Whether replaying this close could end anything (always True when the cache could not be read)."""
    if targets is None:
        return True
    if event_name == TAB_CLOSED:
        return event_data.get("tab_id") in targets.tabs
    if event_name == WORKSPACE_CLOSED:
        return event_data.get("workspace_id") in targets.workspaces
    pane = _canonical_pane(event_data)   # pane.closed or an agent exit
    return pane is not None and pane in targets.panes


def evictable_close(keyed_closes: Iterable[Path], targets: Optional[Targets]) -> Optional[Path]:
    """The oldest spooled close whose replay could end nothing (None: every one may still matter)."""
    if targets is None:
        return None
    for path in sorted(keyed_closes):
        try:
            env = read_json(path)
        except (OSError, ValueError):
            return path   # unreadable: it could never be replayed anyway
        if not isinstance(env, dict) or not isinstance(env.get("event_data"), dict):
            return path
        if not close_is_relevant(env.get("event_name", PANE_CLOSED), env["event_data"], targets):
            return path
    return None
