"""Option R1 flap-tolerant remote SSH watcher and local snapshot self-healing.

* ``collect_snapshots`` runs strictly OUTSIDE the cache lock: discovers enabled SSH
  remotes from ``endpoints.json`` (zero subprocesses when absent) and throttles local
  (10s) and remote (3s) ``herdr api snapshot`` queries.
* ``sync_local_snapshot``, ``sync_remote_endpoint``, ``mark_unconfigured_remotes``, and
  ``expire_unreachable_remotes`` are pure under-lock cache synchronizers (zero subprocess,
  zero network, zero filesystem reads).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from . import clock
from .config import STATUS_MAP
from .herdr_client import (
    RemoteEndpoint,
    _guard_lock_free,
    disambiguate_remote_host,
    discover_enabled_remotes,
    fetch_herdr_snapshot,
)
from .intake import Identity, build_session_id, container_id, resolve_host, resolve_identity
from .lifecycle import expire_session, is_live, number
from .markers import remove_pane_marker
from .multihost import is_local_host
from .sanitize import format_agent_name, sanitize_agent, sanitize_cwd, sanitize_title
from .sender.lease import peek_cache
from .staging import stage_pane_close_result, stage_status

REMOTE_UNREACHABLE_GRACE_SECONDS = 300.0
REMOTE_POLL_SECONDS = 3.0
LOCAL_SNAPSHOT_INTERVAL_SECONDS = 10.0
SNAPSHOT_SYNC_EVENT = "snapshot.sync"
_THROTTLE_TOLERANCE_SECONDS = 1e-3

_last_local_fetch_mono: Optional[float] = None
_last_remote_fetch_mono: Optional[float] = None


@dataclass(frozen=True)
class SnapshotBundle:
    local_snapshot: Optional[dict] = None
    remote_snapshots: Tuple[Tuple[RemoteEndpoint, Optional[dict]], ...] = ()
    remotes_configured: bool = False
    enabled_remote_hosts: Tuple[str, ...] = ()
    remotes_checked: bool = False
    fetched_at_wall: float = 0.0
    fetched_at_ns: int = 0


def reset_watcher_timers() -> None:
    """Reset module-level monotonic fetch throttles (for deterministic tests)."""
    global _last_local_fetch_mono, _last_remote_fetch_mono
    _last_local_fetch_mono = None
    _last_remote_fetch_mono = None


def _interval_due(last_mono: Optional[float], mono_now: float, interval: float) -> bool:
    if last_mono is None or mono_now < last_mono:
        return True
    return (mono_now - last_mono) + _THROTTLE_TOLERANCE_SECONDS >= interval


def has_local_live_sessions(state_dir: Path) -> bool:
    """Lock-free peek for any live, non-salvaged local session in active-sessions.json."""
    _guard_lock_free()
    peek = peek_cache(state_dir)
    sessions = peek.get("sessions")
    if not isinstance(sessions, Mapping) or not sessions:
        return False
    local_host = resolve_host(peek.get("host"))
    return any(
        isinstance(rec, Mapping)
        and is_live(rec)
        and not rec.get("salvaged")
        and is_local_host(rec.get("host"), local_host)
        for rec in sessions.values()
    )


def collect_snapshots(
    *,
    herdr_alive: bool = True,
    has_local_live_sessions: bool = False,
    force_local: bool = False,
    force_remote: bool = False,
    discover_fn: Callable[[], Tuple[RemoteEndpoint, ...]] = discover_enabled_remotes,
    fetch_fn: Callable[[Optional[str]], Optional[dict]] = fetch_herdr_snapshot,
) -> SnapshotBundle:
    """Collect due local and remote snapshots BEFORE acquiring the session cache lock."""
    global _last_local_fetch_mono, _last_remote_fetch_mono
    _guard_lock_free()
    remotes = discover_fn()
    remotes_configured = bool(remotes)
    enabled_hosts = tuple(ep.host for ep in remotes)
    fetched_wall = clock.time()
    fetched_ns = clock.time_ns()
    mono_now = clock.monotonic()

    local_snap: Optional[dict] = None
    remote_snaps: List[Tuple[RemoteEndpoint, Optional[dict]]] = []

    if herdr_alive:
        if remotes and (force_remote or _interval_due(_last_remote_fetch_mono, mono_now, REMOTE_POLL_SECONDS)):
            _last_remote_fetch_mono = mono_now
            for ep in remotes:
                remote_snaps.append((ep, fetch_fn(ep.machine_arg)))
        if has_local_live_sessions and (
            force_local or _interval_due(_last_local_fetch_mono, mono_now, LOCAL_SNAPSHOT_INTERVAL_SECONDS)
        ):
            _last_local_fetch_mono = mono_now
            local_snap = fetch_fn(None)

    return SnapshotBundle(
        local_snapshot=local_snap,
        remote_snapshots=tuple(remote_snaps),
        remotes_configured=remotes_configured,
        enabled_remote_hosts=enabled_hosts,
        remotes_checked=True,
        fetched_at_wall=fetched_wall,
        fetched_at_ns=fetched_ns,
    )


def _workspace_labels(snapshot: Mapping) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for ws in snapshot.get("workspaces") or ():
        if isinstance(ws, Mapping) and isinstance(ws.get("workspace_id"), str) and ws["workspace_id"]:
            out[ws["workspace_id"]] = sanitize_title(ws.get("label"))
    return out


def sync_local_snapshot(
    data: dict,
    snapshot: Optional[dict],
    now_wall: float,
    now_ns: int,
    *,
    herdr_alive: Callable[[], bool] = lambda: True,
) -> Tuple[bool, Tuple[Tuple[str, dict], ...]]:
    """Reconcile cached live local sessions against a ``herdr api snapshot`` under the cache lock."""
    if not isinstance(snapshot, Mapping) or not isinstance(snapshot.get("panes"), list):
        return False, ()

    ws_labels = _workspace_labels(snapshot)
    focused_ctx = {"focused_pane_id": snapshot.get("focused_pane_id")}
    panes_by_canon: Dict[str, Tuple[Identity, Mapping]] = {}
    for p in snapshot["panes"]:
        if not isinstance(p, Mapping):
            continue
        ident, _ = resolve_identity(p, focused_ctx, env={})
        if ident is not None:
            panes_by_canon[ident.canonical_pane] = (ident, p)

    local_host = resolve_host(data.get("host"))
    changed = False
    exports: List[Tuple[str, dict]] = []

    for sid, rec in list(data.get("sessions", {}).items()):
        if not isinstance(rec, dict) or not is_live(rec) or rec.get("salvaged"):
            continue
        if not is_local_host(rec.get("host"), local_host):
            continue
        pane_id = rec.get("pane_id")
        if not isinstance(pane_id, str) or not pane_id:
            continue
        if int(rec.get("admitted_at_ns") or 0) > now_ns or int(rec.get("last_arrival_ns") or 0) > now_ns:
            continue

        if pane_id not in panes_by_canon:
            closed = stage_pane_close_result(
                data,
                pane_id,
                {
                    "pane_id": pane_id,
                    "workspace_id": rec.get("workspace_id") or pane_id.split(":", 1)[0],
                },
                now_ns,
                herdr_alive=herdr_alive,
            )
            if closed.recorded:
                remove_pane_marker(pane_id)
            if closed.targets or closed.recorded:
                changed = True
            continue

        ident, p = panes_by_canon[pane_id]
        arr_time = max(now_wall, now_ns / 1e9, float(rec.get("last_applied_arrival_time") or 0.0))

        if p.get("agent_status") == "idle" and not sanitize_agent(p.get("agent")):
            st = stage_status(
                data,
                ident,
                {
                    "pane_id": pane_id,
                    "workspace_id": ident.workspace_id,
                    "agent": "",
                    "agent_status": "idle",
                },
                {},
                now_ns,
                arr_time,
                herdr_alive=herdr_alive,
                now_wall=now_wall,
                can_export_orphans=True,
            )
            if st.staged:
                changed = True
                exports.extend(st.orphan_exports)
            continue

        fresh_cwd = sanitize_cwd(p.get("foreground_cwd") or p.get("cwd") or "")
        fresh_title = sanitize_title(
            p.get("title")
            or p.get("label")
            or ws_labels.get(ident.workspace_id)
            or p.get("terminal_title_stripped")
            or ""
        )
        fresh_tab = container_id(p, "tab_id")
        raw_ag = sanitize_agent(p.get("agent")) or sanitize_agent(rec.get("raw_agent"))
        mapped = STATUS_MAP.get(str(p.get("agent_status") or ""))

        old_state = rec.get("desired_state")
        old_agent = rec.get("agent")
        old_title = rec.get("title") or ""
        old_cwd = rec.get("cwd") or ""
        old_tab = rec.get("tab_id")

        new_cwd = fresh_cwd if fresh_cwd else old_cwd
        new_title = fresh_title if fresh_title else old_title
        new_tab = fresh_tab if fresh_tab else old_tab
        new_state = mapped if (mapped and raw_ag) else old_state
        new_agent = format_agent_name(raw_ag, display_agent=p.get("display_agent")) if raw_ag else old_agent

        if (new_state, new_agent, new_title, new_cwd, new_tab) != (
            old_state,
            old_agent,
            old_title,
            old_cwd,
            old_tab,
        ):
            rec["cwd"] = new_cwd
            rec["title"] = new_title
            rec["tab_id"] = new_tab
            rec["desired_state"] = new_state
            rec["agent"] = new_agent
            rec["raw_agent"] = raw_ag or rec.get("raw_agent")
            if new_state != old_state:
                rec["last_event_at"] = now_wall
            seq = int(rec.get("seq", 0) or 0) + 1
            rec["seq"] = seq
            rec["desired_payload"] = {
                "state": new_state,
                "agent": new_agent,
                "session_id": sid,
                "title": new_title,
                "cwd": new_cwd,
                "terminal": "Herdr",
                "event": SNAPSHOT_SYNC_EVENT,
                "seq": seq,
            }
            rec["delivery_status"] = "in_flight"
            rec["delivery_error"] = None
            rec["delivery_attempts"] = 0
            rec["next_retry_at"] = None
            changed = True

    return changed, tuple(exports)


def _has_live_for_host(sessions: Mapping, host: str) -> bool:
    return any(isinstance(rec, Mapping) and is_live(rec) and rec.get("host") == host for rec in sessions.values())


def mark_unconfigured_remotes(
    data: dict,
    enabled_remote_hosts: Iterable[str],
    now_wall: float,
) -> bool:
    """Stamp ``remote_unreachable_since`` for any cached live remote host no longer enabled in ``endpoints.json``."""
    local_host = resolve_host(data.get("host"))
    enabled = {
        disambiguate_remote_host(h, h, local_host)
        for h in enabled_remote_hosts
        if isinstance(h, str) and h
    }
    sessions = data.get("sessions", {})
    if not isinstance(sessions, Mapping) or not sessions:
        return False
    unreach = data.setdefault("remote_unreachable_since", {})
    changed = False
    for rec in sessions.values():
        if not isinstance(rec, Mapping) or not is_live(rec):
            continue
        rec_host = rec.get("host")
        if not isinstance(rec_host, str) or is_local_host(rec_host, local_host):
            continue
        if rec_host not in enabled and number(unreach.get(rec_host)) is None:
            unreach[rec_host] = now_wall
            changed = True
    return changed


def expire_unreachable_remotes(data: dict, now_wall: float) -> Tuple[str, ...]:
    """Expire live sessions on any remote host unreachable for > 300s and prune drained entries."""
    unreach = data.get("remote_unreachable_since")
    if not isinstance(unreach, dict) or not unreach:
        return ()
    sessions = data.get("sessions", {})
    expired: List[str] = []
    for host, since_raw in list(unreach.items()):
        if not _has_live_for_host(sessions, host):
            unreach.pop(host, None)
            continue
        since = number(since_raw)
        if since is None or since > now_wall:
            unreach[host] = now_wall
            continue
        if now_wall - since <= REMOTE_UNREACHABLE_GRACE_SECONDS:
            continue
        for sid, rec in list(sessions.items()):
            if isinstance(rec, dict) and is_live(rec) and rec.get("host") == host:
                expire_session(sid, rec, now_wall, "remote_unreachable")
                expired.append(sid)
        if not _has_live_for_host(sessions, host):
            unreach.pop(host, None)
    return tuple(expired)


def sync_remote_endpoint(
    data: dict,
    endpoint: RemoteEndpoint,
    snapshot: Optional[dict],
    now_wall: float,
    now_ns: int,
    *,
    herdr_alive: Callable[[], bool] = lambda: True,
) -> Tuple[bool, Tuple[Tuple[str, dict], ...]]:
    """Reconcile one remote SSH endpoint's snapshot (with 300s flap tolerance) under the cache lock."""
    local_host = resolve_host(data.get("host"))
    eff_host = disambiguate_remote_host(endpoint.host, endpoint.id, local_host)

    if not isinstance(snapshot, Mapping) or not isinstance(snapshot.get("panes"), list):
        sessions = data.get("sessions", {})
        unreach = data.setdefault("remote_unreachable_since", {})
        if not _has_live_for_host(sessions, eff_host):
            popped = unreach.pop(eff_host, None) is not None
            return popped, ()
        since = number(unreach.get(eff_host))
        if since is None or since > now_wall:
            unreach[eff_host] = now_wall
            return True, ()
        if now_wall - since <= REMOTE_UNREACHABLE_GRACE_SECONDS:
            return False, ()
        expired_any = False
        for sid, rec in list(sessions.items()):
            if isinstance(rec, dict) and is_live(rec) and rec.get("host") == eff_host:
                expire_session(sid, rec, now_wall, "remote_unreachable")
                expired_any = True
        if not _has_live_for_host(sessions, eff_host):
            unreach.pop(eff_host, None)
        return expired_any, ()

    unreach_map = data.get("remote_unreachable_since")
    changed = False
    if isinstance(unreach_map, dict) and eff_host in unreach_map:
        unreach_map.pop(eff_host, None)
        changed = True

    ws_labels = _workspace_labels(snapshot)
    focused_ctx = {"focused_pane_id": snapshot.get("focused_pane_id")}
    active_panes: set[str] = set()
    exports: List[Tuple[str, dict]] = []

    for p in snapshot.get("panes") or ():
        if not isinstance(p, Mapping):
            continue
        ident, _ = resolve_identity(p, focused_ctx, env={})
        if ident is None:
            continue
        raw_ag = sanitize_agent(p.get("agent"))
        raw_st = p.get("agent_status")
        mapped = STATUS_MAP.get(str(raw_st or ""))
        if not raw_ag or not mapped:
            continue

        active_panes.add(ident.canonical_pane)
        sid = build_session_id(eff_host, ident.canonical_pane)
        if not sid:
            continue

        existing = data.get("sessions", {}).get(sid)
        existing_live = isinstance(existing, dict) and is_live(existing) and not existing.get("salvaged")
        raw_cwd = sanitize_cwd(p.get("foreground_cwd") or p.get("cwd") or "")
        effective_cwd = raw_cwd or (existing.get("cwd") if existing_live else "") or ""
        raw_title = sanitize_title(
            p.get("title")
            or p.get("label")
            or ws_labels.get(ident.workspace_id)
            or p.get("terminal_title_stripped")
            or ""
        )
        effective_title = (
            raw_title
            or (existing.get("title") if existing_live else "")
            or sanitize_title(f"Pane {ident.canonical_pane}")
        )
        raw_tab = container_id(p, "tab_id")
        effective_tab = raw_tab or (container_id(existing, "tab_id") if existing_live else None)
        agent_name = format_agent_name(raw_ag, remote_host=eff_host, display_agent=p.get("display_agent"))

        if existing_live and (
            existing.get("desired_state"),
            existing.get("agent"),
            existing.get("title") or "",
            existing.get("cwd") or "",
            existing.get("tab_id"),
        ) == (mapped, agent_name, effective_title, effective_cwd, effective_tab):
            continue

        arr_ns = max(now_ns, int(existing.get("last_arrival_ns") or 0) if isinstance(existing, dict) else 0)
        arr_time = max(
            now_wall,
            arr_ns / 1e9,
            float(existing.get("last_applied_arrival_time") or 0.0) if isinstance(existing, dict) else 0.0,
        )
        ev_data: dict = {
            "pane_id": ident.canonical_pane,
            "workspace_id": ident.workspace_id,
            "host": eff_host,
            "agent": raw_ag,
            "display_agent": p.get("display_agent"),
            "agent_status": raw_st,
            "title": effective_title,
            "cwd": effective_cwd,
        }
        if effective_tab is not None:
            ev_data["tab_id"] = effective_tab

        st = stage_status(
            data,
            ident,
            ev_data,
            {},
            arr_ns,
            arr_time,
            herdr_alive=herdr_alive,
            now_wall=now_wall,
            can_export_orphans=True,
        )
        if st.staged:
            changed = True
            exports.extend(st.orphan_exports)

    for sid, rec in list(data.get("sessions", {}).items()):
        if not isinstance(rec, dict) or not is_live(rec):
            continue
        if rec.get("host") != eff_host:
            continue
        pane_id = rec.get("pane_id")
        if not isinstance(pane_id, str) or not pane_id or pane_id in active_panes:
            continue
        close_ns = max(now_ns, int(rec.get("admitted_at_ns") or 0))
        closed = stage_pane_close_result(
            data,
            pane_id,
            {
                "pane_id": pane_id,
                "workspace_id": rec.get("workspace_id") or pane_id.split(":", 1)[0],
                "host": eff_host,
            },
            close_ns,
            herdr_alive=herdr_alive,
        )
        if closed.targets:
            changed = True

    return changed, tuple(exports)


__all__ = [
    "LOCAL_SNAPSHOT_INTERVAL_SECONDS",
    "REMOTE_POLL_SECONDS",
    "REMOTE_UNREACHABLE_GRACE_SECONDS",
    "SNAPSHOT_SYNC_EVENT",
    "SnapshotBundle",
    "collect_snapshots",
    "expire_unreachable_remotes",
    "has_local_live_sessions",
    "mark_unconfigured_remotes",
    "reset_watcher_timers",
    "sync_local_snapshot",
    "sync_remote_endpoint",
]
