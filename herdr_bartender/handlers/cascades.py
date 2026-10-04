"""tab.closed / workspace.closed container cascades."""

from __future__ import annotations

import os

from .. import clock
from ..bridge import post_bartender_event
from ..cache import BoundedSessionCache
from ..intake import (
    TAB_CLOSED,
    WORKSPACE_CLOSED,
    build_close_payload,
    container_id,
    loggable,
    session_matches_tab,
    session_matches_workspace,
)
from ..log import log_debug
from ..markers import remove_pane_marker, touch_heartbeat, touch_pane_failed
from ..orphans import export_orphan_record, remove_orphan_record
from ..paths import get_state_dir
from ..process import is_process_instance_alive, own_start_time
from ..sender import ensure_reconciler_running, touch_reconciler_pending
from ..vendor import cleanup_vendor_active


def handle_tab_closed(event_data: dict, context: dict, bridge_url: str | None = None, arrival_ns: int | None = None, spool_generation: int | None = None):
    touch_heartbeat()
    arr_ns = arrival_ns or clock.time_ns()
    event_data = event_data if isinstance(event_data, dict) else {}
    # Exact container ids from event data only; no focused-context fallback (§2.1, gap cascade-container-matching).
    tab_id = container_id(event_data, "tab_id")
    if not tab_id:
        log_debug(f"Ignoring tab.closed without a valid tab_id: {loggable(event_data.get('tab_id'))}")
        return
    closed_ws = container_id(event_data, "workspace_id")

    cache_mgr = BoundedSessionCache(get_state_dir())
    target_sessions = []
    now_wall = clock.time()
    my_pid = os.getpid()

    with cache_mgr as data:
        sessions = data.get("sessions", {})
        for sid, info in list(sessions.items()):
            if session_matches_tab(info, tab_id, closed_ws):
                if spool_generation is not None and info.get("generation", 1) > spool_generation:
                    continue
                if info.get("admitted_at_ns", 0) > arr_ns:
                    continue
                seq = int(info.get("seq", 0)) + 1
                info["desired_state"] = "Ended"
                info["seq"] = seq
                info["closed_at_ns"] = arr_ns
                info["close_kind"] = "container"
                close_payload = build_close_payload(sid, info, TAB_CLOSED, seq)
                info["desired_payload"] = close_payload
                lease_deadline = info.get("lease_deadline") or 0.0
                sending_pid = info.get("sending_pid")
                l_tok = info.get("lease_token") or ""
                l_parts = l_tok.split(":")
                holder_st = l_parts[1] if len(l_parts) >= 2 else None
                is_active = (sending_pid is not None) and (sending_pid != my_pid) and is_process_instance_alive(sending_pid, holder_st) and (now_wall < lease_deadline + 0.5)
                if not is_active:
                    my_token = f"{my_pid}:{own_start_time()}:{clock.time()}:{sid}"
                    info["lease_token"] = my_token
                    info["sending_pid"] = my_pid
                    info["lease_deadline"] = now_wall + 1.5
                    my_resync_gen = info.get("resync_generation", 0)
                    info["lease_resync_gen"] = my_resync_gen
                    target_sessions.append((sid, info.get("pane_id"), close_payload, seq, my_token, my_resync_gen))
                    if info.get("pane_id"):
                        p_pane = info.get("pane_id")
                        closed_source_ts = max(float(event_data.get("timestamp") or 0.0), info.get("last_source_timestamp", 0.0), float(arr_ns) / 1e9)
                        info["closed_source_ts"] = closed_source_ts
                        data.setdefault("tombstones", {})[p_pane] = {
                            "closed_at_ns": arr_ns,
                            "closed_source_ts": closed_source_ts,
                            "last_source_timestamp": max(info.get("last_source_timestamp", 0.0), float(event_data.get("timestamp") or 0.0)),
                        }
                else:
                    touch_reconciler_pending()
                    ensure_reconciler_running()
        if sessions:
            cache_mgr.save(data)

    sync_sessions = target_sessions[:1]
    overflow_sessions = target_sessions[1:]

    if overflow_sessions:
        with cache_mgr as data:
            for sid, _, _, _, _, _ in overflow_sessions:
                s = data.get("sessions", {}).get(sid)
                if s:
                    s["sending_pid"] = None
                    s["lease_token"] = None
                    s["lease_deadline"] = None
            cache_mgr.save(data)
        ensure_reconciler_running()

    for sid, pane_id, close_payload, target_seq, my_token, my_resync_gen in sync_sessions:
        success, is_non_retryable = post_bartender_event(close_payload, bridge_url=bridge_url)
        vendor_to_clean = None
        orphans_to_export = []
        orphans_to_remove = []
        should_spawn_reconciler = False
        with cache_mgr as data:
            s = data.get("sessions", {}).get(sid)
            if not s:
                continue
            if s.get("lease_token") != my_token:
                log_debug(f"Lease token superseded for {sid} during tab.closed, forcing re-sync")
                s["delivered_seq"] = 0
                s["delivery_status"] = "in_flight"
                s["resync_generation"] = s.get("resync_generation", 0) + 1
                touch_reconciler_pending()
                should_spawn_reconciler = True
                cache_mgr.save(data)
                continue

            s["sending_pid"] = None
            s["lease_token"] = None
            s["lease_deadline"] = None

            if success:
                if s.get("resync_generation", 0) > my_resync_gen:
                    log_debug(f"Superseding sender landed during tab.closed for {sid}, forcing re-sync")
                    s["delivered_seq"] = 0
                    s["delivery_status"] = "in_flight"
                    touch_reconciler_pending()
                    should_spawn_reconciler = True
                    cache_mgr.save(data)
                    continue

                if s.get("desired_state") == "Ended" and s.get("seq") == target_seq:
                    data.get("sessions", {}).pop(sid, None)
                    remove_pane_marker(pane_id)
                    orphans_to_remove.append(sid)
                    if pane_id and s.get("close_kind") == "container":
                        data.setdefault("tombstones", {})[pane_id] = {
                            "closed_at_ns": s.get("closed_at_ns") or arr_ns,
                            "closed_source_ts": s.get("closed_source_ts", 0.0),
                            "last_source_timestamp": s.get("last_source_timestamp", 0.0),
                        }
                    vendor_to_clean = pane_id
            elif is_non_retryable:
                s["delivery_status"] = "non_retryable_failed"
                s["rejected_seq"] = target_seq
                s["delivery_error"] = "bridge_rejected"
                touch_pane_failed(pane_id)
                if s.get("desired_state") == "Ended":
                    s["orphaned_ended"] = True
                    orphans_to_export.append((sid, dict(s)))
            else:
                s["delivery_attempts"] = s.get("delivery_attempts", 0) + 1
                s["delivery_error"] = "retryable network error"
                touch_pane_failed(pane_id)
                if s.get("delivery_attempts", 0) >= 5:
                    s["delivery_status"] = "retryable_exhausted"
                    if s.get("desired_state") == "Ended":
                        s["orphaned_ended"] = True
                        orphans_to_export.append((sid, dict(s)))
            cache_mgr.save(data)

        for o_sid, o_info in orphans_to_export:
            export_orphan_record(o_sid, o_info)
        for o_sid in orphans_to_remove:
            remove_orphan_record(o_sid)
        if should_spawn_reconciler:
            ensure_reconciler_running()
        if vendor_to_clean:
            cleanup_vendor_active(vendor_to_clean, is_pane_closed=True, bridge_url=bridge_url)


def handle_workspace_closed(event_data: dict, context: dict, bridge_url: str | None = None, arrival_ns: int | None = None, spool_generation: int | None = None):
    touch_heartbeat()
    arr_ns = arrival_ns or clock.time_ns()
    event_data = event_data if isinstance(event_data, dict) else {}
    workspace_id = container_id(event_data, "workspace_id")
    if not workspace_id:
        log_debug(f"Ignoring workspace.closed without a valid workspace_id: {loggable(event_data.get('workspace_id'))}")
        return

    cache_mgr = BoundedSessionCache(get_state_dir())
    target_sessions = []
    now_wall = clock.time()
    my_pid = os.getpid()

    with cache_mgr as data:
        sessions = data.get("sessions", {})
        for sid, info in list(sessions.items()):
            if session_matches_workspace(info, workspace_id):
                if spool_generation is not None and info.get("generation", 1) > spool_generation:
                    continue
                if info.get("admitted_at_ns", 0) > arr_ns:
                    continue
                seq = int(info.get("seq", 0)) + 1
                info["desired_state"] = "Ended"
                info["seq"] = seq
                info["closed_at_ns"] = arr_ns
                closed_source_ts = max(float(event_data.get("timestamp") or 0.0), info.get("last_source_timestamp", 0.0), float(arr_ns) / 1e9)
                info["closed_source_ts"] = closed_source_ts
                info["close_kind"] = "container"
                close_payload = build_close_payload(sid, info, WORKSPACE_CLOSED, seq)
                info["desired_payload"] = close_payload
                lease_deadline = info.get("lease_deadline") or 0.0
                sending_pid = info.get("sending_pid")
                l_tok = info.get("lease_token") or ""
                l_parts = l_tok.split(":")
                holder_st = l_parts[1] if len(l_parts) >= 2 else None
                is_active = (sending_pid is not None) and (sending_pid != my_pid) and is_process_instance_alive(sending_pid, holder_st) and (now_wall < lease_deadline + 0.5)
                if not is_active:
                    my_token = f"{my_pid}:{own_start_time()}:{clock.time()}:{sid}"
                    info["lease_token"] = my_token
                    info["sending_pid"] = my_pid
                    info["lease_deadline"] = now_wall + 1.5
                    my_resync_gen = info.get("resync_generation", 0)
                    target_sessions.append((sid, info.get("pane_id"), close_payload, seq, my_token, my_resync_gen))
                    if info.get("pane_id"):
                        p_pane = info.get("pane_id")
                        data.setdefault("tombstones", {})[p_pane] = {
                            "closed_at_ns": arr_ns,
                            "closed_source_ts": closed_source_ts,
                            "last_source_timestamp": max(info.get("last_source_timestamp", 0.0), float(event_data.get("timestamp") or 0.0)),
                        }
                else:
                    touch_reconciler_pending()
                    ensure_reconciler_running()
        if sessions:
            cache_mgr.save(data)

    sync_sessions = target_sessions[:1]
    overflow_sessions = target_sessions[1:]

    if overflow_sessions:
        with cache_mgr as data:
            for sid, _, _, _, _, _ in overflow_sessions:
                s = data.get("sessions", {}).get(sid)
                if s:
                    s["sending_pid"] = None
                    s["lease_token"] = None
                    s["lease_deadline"] = None
            cache_mgr.save(data)
        ensure_reconciler_running()

    for sid, pane_id, close_payload, target_seq, my_token, my_resync_gen in sync_sessions:
        success, is_non_retryable = post_bartender_event(close_payload, bridge_url=bridge_url)
        vendor_to_clean = None
        orphans_to_export = []
        orphans_to_remove = []
        should_spawn_reconciler = False
        with cache_mgr as data:
            s = data.get("sessions", {}).get(sid)
            if not s:
                continue
            if s.get("lease_token") != my_token:
                log_debug(f"Lease token superseded for {sid} during workspace.closed, forcing re-sync")
                s["resync_generation"] = s.get("resync_generation", 0) + 1
                s["delivered_seq"] = 0
                s["delivery_status"] = "in_flight"
                touch_reconciler_pending()
                should_spawn_reconciler = True
                cache_mgr.save(data)
                continue

            s["sending_pid"] = None
            s["lease_token"] = None
            s["lease_deadline"] = None

            if success:
                if s.get("resync_generation", 0) > my_resync_gen:
                    log_debug(f"Resync generation superseded for {sid} during workspace.closed, forcing re-sync")
                    s["delivered_seq"] = 0
                    s["delivery_status"] = "in_flight"
                    touch_reconciler_pending()
                    should_spawn_reconciler = True
                elif s.get("desired_state") == "Ended" and s.get("seq") == target_seq:
                    data.get("sessions", {}).pop(sid, None)
                    remove_pane_marker(pane_id)
                    orphans_to_remove.append(sid)
                    if pane_id and s.get("close_kind") == "container":
                        data.setdefault("tombstones", {})[pane_id] = {
                            "closed_at_ns": s.get("closed_at_ns") or arr_ns,
                            "closed_source_ts": s.get("closed_source_ts", 0.0),
                            "last_source_timestamp": s.get("last_source_timestamp", 0.0),
                        }
                    vendor_to_clean = pane_id
            elif is_non_retryable:
                s["delivery_status"] = "non_retryable_failed"
                s["rejected_seq"] = target_seq
                s["delivery_error"] = "bridge_rejected"
                touch_pane_failed(pane_id)
                if s.get("desired_state") == "Ended":
                    s["orphaned_ended"] = True
                    orphans_to_export.append((sid, dict(s)))
            else:
                s["delivery_attempts"] = s.get("delivery_attempts", 0) + 1
                s["delivery_error"] = "retryable network error"
                touch_pane_failed(pane_id)
                if s.get("delivery_attempts", 0) >= 5:
                    s["delivery_status"] = "retryable_exhausted"
                    if s.get("desired_state") == "Ended":
                        s["orphaned_ended"] = True
                        orphans_to_export.append((sid, dict(s)))
            cache_mgr.save(data)

        for o_sid, o_info in orphans_to_export:
            export_orphan_record(o_sid, o_info)
        for o_sid in orphans_to_remove:
            remove_orphan_record(o_sid)
        if should_spawn_reconciler:
            ensure_reconciler_running()
        if vendor_to_clean:
            cleanup_vendor_active(vendor_to_clean, is_pane_closed=True, bridge_url=bridge_url)
