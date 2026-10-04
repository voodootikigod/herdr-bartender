"""pane.closed handler."""

from __future__ import annotations

import os

from .. import clock
from ..bridge import post_bartender_event
from ..cache import BoundedSessionCache
from ..config import PANE_ID_REGEX
from ..log import log_debug
from ..markers import remove_pane_marker, touch_heartbeat, touch_pane_failed
from ..orphans import export_orphan_record, remove_orphan_record
from ..paths import get_state_dir
from ..process import is_process_instance_alive, own_start_time
from ..sanitize import normalize_pane_id
from ..sender import ensure_reconciler_running, touch_reconciler_pending
from ..vendor import cleanup_vendor_active


def handle_pane_closed(event_data: dict, context: dict, bridge_url: str | None = None, arrival_ns: int | None = None, spool_generation: int | None = None):
    touch_heartbeat()
    arr_ns = arrival_ns or clock.time_ns()
    raw_pane = event_data.get("pane_id") or context.get("focused_pane_id")
    workspace_id = event_data.get("workspace_id") or context.get("workspace_id")
    if not workspace_id and ":" in raw_pane:
        workspace_id = raw_pane.split(":", 1)[0]
    if ":" not in raw_pane and not workspace_id:
        log_debug(f"Rejecting colon-less pane ID {raw_pane} without workspace_id")
        return

    canonical_pane = normalize_pane_id(raw_pane, workspace_id)
    if not PANE_ID_REGEX.match(canonical_pane):
        log_debug(f"Invalid canonical pane ID: {canonical_pane}")
        return
    cache_mgr = BoundedSessionCache(get_state_dir())
    target_sessions = []
    now_wall = clock.time()
    my_pid = os.getpid()

    with cache_mgr as data:
        sessions = data.get("sessions", {})
        matching_last_source_ts = 0.0
        for sid, info in sessions.items():
            if info.get("pane_id") == canonical_pane or sid.endswith(f":{canonical_pane}"):
                ts = info.get("last_source_timestamp", 0.0)
                if ts and ts > matching_last_source_ts:
                    matching_last_source_ts = ts
        closed_source_ts = max(float(event_data.get("timestamp") or 0.0), matching_last_source_ts, float(arr_ns) / 1e9)
        data.setdefault("tombstones", {})[canonical_pane] = {
            "closed_at_ns": arr_ns,
            "closed_source_ts": closed_source_ts,
            "last_source_timestamp": max(matching_last_source_ts, float(event_data.get("timestamp") or 0.0)),
        }

        for sid, info in list(sessions.items()):
            if info.get("pane_id") == canonical_pane or sid.endswith(f":{canonical_pane}"):
                if spool_generation is not None and info.get("generation", 1) > spool_generation:
                    log_debug(f"Ignoring spooled close with older generation {spool_generation} < {info.get('generation')}")
                    continue
                if info.get("admitted_at_ns", 0) > arr_ns:
                    log_debug(f"Ignoring close event predating session admission: close {arr_ns} < admitted {info.get('admitted_at_ns')}")
                    continue
                seq = int(info.get("seq", 0)) + 1
                info["desired_state"] = "Ended"
                info["seq"] = seq
                info["closed_at_ns"] = arr_ns
                info["close_kind"] = "container"
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
                    target_sessions.append((sid, canonical_pane, info.get("agent", "Herdr"), seq, my_token, my_resync_gen))
                else:
                    touch_reconciler_pending()
                    ensure_reconciler_running()
        if sessions:
            cache_mgr.save(data)

    # Dispatch Ended outside lock
    for sid, pane_id, agent_name, target_seq, my_token, my_resync_gen in target_sessions:
        success, is_non_retryable = post_bartender_event(
            {"state": "Ended", "agent": agent_name, "session_id": sid},
            bridge_url=bridge_url
        )
        vendor_to_clean = None
        orphans_to_export = []
        orphans_to_remove = []
        should_spawn_reconciler = False
        with cache_mgr as data:
            s = data.get("sessions", {}).get(sid)
            if not s:
                continue
            if s.get("lease_token") != my_token:
                log_debug(f"Lease token superseded for {sid} during pane.closed, forcing re-sync")
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
                    log_debug(f"Superseding sender landed during pane.closed for {sid}, forcing re-sync")
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
                    if s.get("close_kind") == "container":
                        data.setdefault("tombstones", {})[canonical_pane] = {
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
