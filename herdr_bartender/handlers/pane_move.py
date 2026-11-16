"""Herdr 0.9.5 ``pane.moved`` staging and handler.

Atomically closes/tombstones the previous canonical pane (when the canonical pane ID
changed) and stages the destination pane's active agent session under one Step A lock
hold, leaving multi-target delivery to the R5 cascade clamp + reconciler hand-off.
"""

from __future__ import annotations

from typing import Callable, Iterable, List, Mapping, Optional, Tuple

from .. import clock
from ..intake import Identity, container_id, resolve_host, resolve_identity, session_matches_pane
from ..log import log_debug
from ..markers import remove_pane_marker, touch_heartbeat
from ..multihost import host_matches_session, resolve_event_host
from ..process import memoised_herdr_alive
from ..sanitize import sanitize_agent, sanitize_cwd, sanitize_title
from ..sender import Stage, Staged, Target
from ..staging import stage_pane_close_result, stage_status
from .flow import run_event

PANE_MOVED = "pane.moved"

_VALID_MOVE_STATUSES = ("working", "blocked", "done", "idle")
_REVERSE_STATUS_MAP = {
    "Working": "working",
    "Waiting": "blocked",
    "Done": "done",
    "Idle": "idle",
}


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _first_clean(candidates: Iterable[object], clean: Callable[[object], str]) -> str:
    for candidate in candidates:
        value = clean(candidate)
        if value:
            return value
    return ""


def _resolve_move_identities(
    event_data: Mapping,
    context: Mapping,
) -> Tuple[Optional[Identity], Optional[Identity], dict, dict]:
    prev_pane = event_data.get("previous_pane_id")
    old_identity, old_notes = resolve_identity(
        {"pane_id": prev_pane, "workspace_id": event_data.get("previous_workspace_id")},
        {},
        env={},
    )
    if prev_pane not in (None, ""):
        for note in old_notes:
            log_debug(note)

    pane_raw = event_data.get("pane")
    pane_dict = dict(pane_raw) if isinstance(pane_raw, Mapping) else {}
    dest_ctx = dict(context) if isinstance(context, Mapping) else {}
    if pane_dict.get("focused") is True and "focused_pane_id" not in dest_ctx and pane_dict.get("pane_id"):
        dest_ctx["focused_pane_id"] = pane_dict.get("pane_id")

    if pane_dict:
        dest_identity, dest_notes = resolve_identity(pane_dict, dest_ctx, env={})
        for note in dest_notes:
            log_debug(note)
    else:
        dest_identity = None

    return old_identity, dest_identity, pane_dict, dest_ctx


def _live_session_for_pane(
    data: dict,
    canonical_pane: str,
    host: str,
    local_host: str,
) -> Optional[dict]:
    for sid, rec in data.get("sessions", {}).items():
        if not isinstance(rec, dict):
            continue
        if rec.get("desired_state") == "Ended" or rec.get("salvaged"):
            continue
        if host_matches_session(rec, host, local_host) and session_matches_pane(sid, rec, canonical_pane, host):
            return rec
    return None


def stage_pane_move(
    data: dict,
    event_data: Mapping,
    context: Mapping,
    arr_ns: int,
    arr_time: Optional[float] = None,
    *,
    herdr_alive: Callable[[], bool] = memoised_herdr_alive,
    spool_generation: Optional[int] = None,
    require_newer: bool = False,
    can_export_orphans: bool = False,
) -> Staged:
    """Pure under-lock staging for ``pane.moved`` (no I/O, no network, no subprocess)."""
    event_data = event_data if isinstance(event_data, Mapping) else {}
    context = context if isinstance(context, Mapping) else {}
    old_identity, dest_identity, pane_dict, dest_ctx = _resolve_move_identities(event_data, context)
    if old_identity is None and dest_identity is None:
        return Staged()

    local_host = resolve_host(data.get("host"))
    host_source = event_data if isinstance(event_data.get("host"), str) and event_data["host"].strip() else pane_dict
    host, _ = resolve_event_host(host_source, data.get("host"))

    old_live = _live_session_for_pane(data, old_identity.canonical_pane, host, local_host) if old_identity else None
    old_raw_agent = sanitize_agent(old_live.get("raw_agent")) if old_live else ""
    old_cwd = old_live.get("cwd") if old_live and isinstance(old_live.get("cwd"), str) else ""
    old_title = old_live.get("title") if old_live and isinstance(old_live.get("title"), str) else ""
    old_last_src_ts = old_live.get("last_source_timestamp") if old_live else None
    _ = old_last_src_ts  # retained from pre-close snapshot for inspection symmetry

    same_pane = (
        old_identity is not None
        and dest_identity is not None
        and old_identity.canonical_pane == dest_identity.canonical_pane
    )

    targets: List[Target] = []
    mutated = False
    orphan_exports = ()

    # (a) Close old pane when canonical pane ID changed (or destination is absent).
    if old_identity is not None and not same_pane:
        close_ev: dict = {
            "pane_id": old_identity.canonical_pane,
            "workspace_id": old_identity.workspace_id,
        }
        if "timestamp" in event_data:
            close_ev["timestamp"] = event_data["timestamp"]
        if "host" in event_data:
            close_ev["host"] = event_data["host"]
        elif "host" in pane_dict:
            close_ev["host"] = pane_dict["host"]

        closed = stage_pane_close_result(
            data,
            old_identity.canonical_pane,
            close_ev,
            arr_ns,
            spool_generation,
            herdr_alive=herdr_alive,
        )
        for t in closed.targets:
            t.payload["event"] = PANE_MOVED
            t.record["desired_payload"] = {**t.record["desired_payload"], "event": PANE_MOVED}
        if closed.recorded:
            remove_pane_marker(old_identity.canonical_pane)
        targets.extend(Target(t.session_id, t.pane_id, t.record) for t in closed.targets)
        mutated = True

    # (b) Stage destination pane when it has an active agent (or same-pane live metadata changed).
    if dest_identity is not None:
        agent_status = pane_dict.get("agent_status")
        explicit_agent = sanitize_agent(pane_dict.get("agent"))
        dest_live = old_live if same_pane else _live_session_for_pane(data, dest_identity.canonical_pane, host, local_host)

        if explicit_agent:
            raw_agent = explicit_agent
        elif old_raw_agent and (
            "agent" not in pane_dict
            or (pane_dict.get("agent") is None and agent_status in ("working", "blocked", "done"))
            or (same_pane and pane_dict.get("agent") is None and dest_live is not None)
        ):
            raw_agent = old_raw_agent
        else:
            raw_agent = ""

        fresh_cwd = _first_clean((pane_dict.get("foreground_cwd"), pane_dict.get("cwd"), old_cwd), sanitize_cwd)
        fresh_title = _first_clean(
            (pane_dict.get("title"), pane_dict.get("label"), pane_dict.get("terminal_title_stripped"), old_title),
            sanitize_title,
        )
        fresh_tab = container_id(pane_dict, "tab_id")

        same_pane_updated = bool(
            same_pane
            and dest_live is not None
            and (
                (fresh_cwd and fresh_cwd != dest_live.get("cwd"))
                or (fresh_title and fresh_title != dest_live.get("title"))
                or (fresh_tab and fresh_tab != dest_live.get("tab_id"))
            )
        )
        if agent_status in _VALID_MOVE_STATUSES:
            effective_status = agent_status
        elif same_pane_updated and dest_live is not None:
            effective_status = _REVERSE_STATUS_MAP.get(str(dest_live.get("desired_state")), "idle")
        else:
            effective_status = None

        if raw_agent and effective_status in _VALID_MOVE_STATUSES:
            status_ev: dict = {
                "pane_id": dest_identity.canonical_pane,
                "workspace_id": dest_identity.workspace_id,
                "agent": raw_agent,
                "agent_status": effective_status,
                "display_agent": pane_dict.get("display_agent"),
                "cwd": fresh_cwd,
                "title": fresh_title,
                "tab_id": pane_dict.get("tab_id"),
            }
            if "timestamp" in event_data:
                status_ev["timestamp"] = event_data["timestamp"]
            elif "timestamp" in pane_dict:
                status_ev["timestamp"] = pane_dict["timestamp"]
            if "host" in event_data:
                status_ev["host"] = event_data["host"]
            elif "host" in pane_dict:
                status_ev["host"] = pane_dict["host"]

            staged_dest = stage_status(
                data,
                dest_identity,
                status_ev,
                dest_ctx,
                arr_ns,
                arr_time,
                herdr_alive=herdr_alive,
                spool_generation=spool_generation,
                require_newer=require_newer,
                can_export_orphans=can_export_orphans,
            )
            if staged_dest.staged and staged_dest.record is not None:
                staged_dest.record["desired_payload"] = {
                    **staged_dest.record["desired_payload"],
                    "event": PANE_MOVED,
                }
                targets.append(Target(staged_dest.session_id, dest_identity.canonical_pane, staged_dest.record))
                mutated = True
                orphan_exports = staged_dest.orphan_exports

    return Staged(tuple(targets), mutated=mutated, orphan_exports=orphan_exports)


def _move_stage(event_data: dict, context: dict, arr_ns: int, arr_time: float,
                spool_generation: Optional[int]) -> Stage:
    def stage(data: dict) -> Staged:
        return stage_pane_move(
            data,
            event_data,
            context,
            arr_ns,
            arr_time,
            herdr_alive=memoised_herdr_alive,
            spool_generation=spool_generation,
            can_export_orphans=True,
        )
    return stage


def handle_pane_moved(
    event_data: dict,
    context: dict,
    bridge_url: Optional[str] = None,
    arrival_time: Optional[float] = None,
    arrival_ns: Optional[int] = None,
    spool_generation: Optional[int] = None,
) -> None:
    arr_time = arrival_time or clock.time()
    arr_ns = arrival_ns or clock.time_ns()
    event_data, context = _as_dict(event_data), _as_dict(context)

    def prepare() -> Optional[Stage]:
        touch_heartbeat()
        old_identity, dest_identity, _, _ = _resolve_move_identities(event_data, context)
        if old_identity is None and dest_identity is None:
            return None
        return _move_stage(event_data, context, arr_ns, arr_time, spool_generation)

    run_event(PANE_MOVED, event_data, context, arr_ns, prepare, bridge_url)
