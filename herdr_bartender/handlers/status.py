"""pane.agent_status_changed handler."""

from __future__ import annotations

import os

from .. import clock, runtime
from ..bridge import _raw_post_event, post_bartender_event
from ..cache import BoundedSessionCache
from ..log import log_debug
from ..markers import (
    clear_delivery_down,
    clear_pane_failed,
    remove_pane_marker,
    touch_delivery_down,
    touch_heartbeat,
    touch_pane_failed,
    touch_pane_marker,
)
from ..orphans import export_orphan_record, remove_orphan_record
from ..paths import get_state_dir
from ..process import is_herdr_alive, is_process_instance_alive, own_start_time
from ..intake import (
    admit_status,
    build_session_id,
    classify_status,
    loggable,
    resolve_fields,
    resolve_host,
    resolve_identity,
)
from ..sanitize import format_agent_name
from ..sender import ensure_reconciler_running, touch_reconciler_pending
from ..vendor import cleanup_vendor_active


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def handle_agent_status_changed(event_data: dict, context: dict, bridge_url: str | None = None, arrival_time: float | None = None, arrival_ns: int | None = None, spool_generation: int | None = None):
    touch_heartbeat()
    arr_time = arrival_time or clock.time()
    arr_ns = arrival_ns or clock.time_ns()
    event_data, context = _as_dict(event_data), _as_dict(context)
    agent_status = event_data.get("agent_status")
    identity, notes = resolve_identity(event_data, context)
    for note in notes:
        log_debug(note)
    if identity is None:
        return
    canonical_pane = identity.canonical_pane
    status_kind = classify_status(agent_status)
    if status_kind == "unrecognized":
        log_debug(f"Warning: unrecognized agent_status {loggable(agent_status)} for pane {canonical_pane}")
        return

    cache_mgr = BoundedSessionCache(get_state_dir())
    with cache_mgr as data:
        host = resolve_host(data.get("host"))
    session_id = build_session_id(host, canonical_pane)
    if not session_id:
        log_debug(f"Rejecting out-of-bounds session_id for host {loggable(host)} pane {canonical_pane}")
        return

    # 1. Critical section: Update cache & manage per-session monotonic lease
    with cache_mgr as data:
        tombstone_entry = data.get("tombstones", {}).get(canonical_pane)
        if tombstone_entry:
            if isinstance(tombstone_entry, dict):
                t_closed_ns = tombstone_entry.get("closed_at_ns", 0)
                t_closed_src = tombstone_entry.get("closed_source_ts", 0.0)
                t_src_ts = tombstone_entry.get("last_source_timestamp", 0.0)
            else:
                t_closed_ns = int(tombstone_entry)
                t_closed_src = 0.0
                t_src_ts = 0.0

            if arr_ns <= t_closed_ns:
                log_debug(f"Rejecting late event for closed pane {canonical_pane}: arrival {arr_ns} <= tombstone {t_closed_ns}")
                return

            src_ts = event_data.get("timestamp")
            if src_ts is not None and t_src_ts and float(src_ts) <= t_src_ts:
                log_debug(f"Rejecting trailing event for closed pane {canonical_pane}: source ts {src_ts} <= tombstone ts {t_src_ts}")
                return

            time_since_tombstone_ns = arr_ns - t_closed_ns
            if time_since_tombstone_ns < 60_000_000_000:
                if agent_status != "working":
                    log_debug(f"Rejecting non-working status {agent_status} for recently closed pane {canonical_pane}")
                    return
                raw_agent_input = event_data.get("agent")
                if not raw_agent_input or not str(raw_agent_input).strip() or not is_herdr_alive():
                    log_debug(f"Rejecting event without positive admission signal for recently closed pane {canonical_pane}")
                    return
                if src_ts is not None and (float(src_ts) <= t_closed_src or float(src_ts) <= t_src_ts):
                    log_debug(f"Rejecting pre-close status with source ts {src_ts} <= closed_source_ts {t_closed_src}")
                    return
            data.get("tombstones", {}).pop(canonical_pane, None)

        # Check agent_exits table (agent exited interactive shell without container close)
        if canonical_pane in data.get("agent_exits", {}):
            exit_info = data["agent_exits"][canonical_pane]
            exit_ns = exit_info.get("exit_at_ns", 0)
            exit_src_ts = exit_info.get("exit_source_ts", 0.0)
            src_ts = event_data.get("timestamp")

            if arr_ns - exit_ns > 60_000_000_000:
                data.get("agent_exits", {}).pop(canonical_pane, None)
            elif arr_ns <= exit_ns or (src_ts is not None and float(src_ts) <= exit_src_ts):
                log_debug(f"Rejecting stale event arriving after agent exit for pane {canonical_pane}")
                return
            elif agent_status == "working" and arr_ns > exit_ns:
                data.get("agent_exits", {}).pop(canonical_pane, None)

        sessions = data.get("sessions", {})
        if session_id not in sessions and len(sessions) >= 256:
            log_debug(f"Cache capacity limit of 256 active sessions reached; refusing admission for {session_id}")
            return
        cached_s = sessions.get(session_id, {})

        # Anti-Flap Rule: Herdr may report unknown transiently during reattach/startup.
        if status_kind == "unknown":
            log_debug(f"Debouncing transient unknown status for pane {canonical_pane}")
            return

        # §2.2 admission + Cases A/B/C, then §2.3 field resolution (intake.py)
        admission = admit_status(agent_status, event_data, context, identity, cached_s)
        if admission is None:
            return
        mapped_state, raw_agent = admission
        agent_name = format_agent_name(raw_agent)
        fields = resolve_fields(event_data, context, identity, cached_s)
        title, cwd, tab_id, workspace_id = fields.title, fields.cwd, fields.tab_id, fields.workspace_id

        now_wall = clock.time()
        source_ts = event_data.get("timestamp")
        last_source_ts = cached_s.get("last_source_timestamp", 0)
        if source_ts and last_source_ts and source_ts < last_source_ts:
            return

        session_record = sessions.setdefault(session_id, {})
        last_arr_ns = session_record.get("last_arrival_ns", 0)
        last_applied_arr = session_record.get("last_applied_arrival_time", 0.0)
        # Agent exit to Ended is subject to arrival ordering (not exempt)
        if arr_ns < last_arr_ns or arr_time < last_applied_arr:
            log_debug(f"Dropping older event: arrival {arr_ns} < last_arrival {last_arr_ns}")
            return

        # Generation reset: new admission or agent turn over Ended session resets generation
        # Generation is stored in root-level pane_generations so eviction does not reset it
        is_new_turn = cached_s.get("desired_state") == "Ended" or cached_s.get("delivered_state") == "Ended" or "generation" not in session_record
        if is_new_turn:
            curr_next_gen = int(data.get("next_generation", 1))
            pane_gens = data.setdefault("pane_generations", {})
            curr_gen = max(curr_next_gen, int(pane_gens.get(canonical_pane, 0)), int(session_record.get("generation", 0))) + 1
            data["next_generation"] = curr_gen
            pane_gens[canonical_pane] = curr_gen
            session_record["generation"] = curr_gen
            session_record["admitted_at_ns"] = arr_ns
        session_record["last_event_ns"] = arr_ns
        session_record["last_arrival_ns"] = arr_ns
        session_record["salvaged"] = False

        if spool_generation is not None and session_record.get("generation", 1) > spool_generation:
            log_debug(f"Ignoring spooled event with older generation {spool_generation} < {session_record.get('generation')}")
            return

        new_seq = int(session_record.get("seq", 0)) + 1

        payload = {
            "state": mapped_state,
            "agent": agent_name,
            "session_id": session_id,
            "title": title,
            "cwd": cwd,
            "terminal": "Herdr",
            "event": "pane.agent_status_changed",
            "seq": new_seq,
        }

        session_record.update({
            "pane_id": canonical_pane,
            "workspace_id": workspace_id,
            "tab_id": tab_id,
            "host": host,
            "agent": agent_name,
            "raw_agent": raw_agent,
            "title": title,
            "cwd": cwd,
            "desired_state": mapped_state,
            "desired_payload": payload,
            "seq": new_seq,
            "delivery_status": "in_flight",
            "delivery_error": None,
            "delivery_attempts": 0,
            "last_applied_arrival_time": arr_time,
            "last_arrival_ns": arr_ns,
            "last_event_ns": arr_ns,
            "last_event_at": now_wall,
        })
        if mapped_state == "Ended":
            session_record["close_kind"] = "agent_exit"
            session_record["exit_at_ns"] = arr_ns
            session_record["exit_source_ts"] = float(event_data.get("timestamp") or 0.0)
            data.setdefault("agent_exits", {})[canonical_pane] = {
                "exit_at_ns": arr_ns,
                "exit_source_ts": float(event_data.get("timestamp") or 0.0),
            }
        if source_ts:
            session_record["last_source_timestamp"] = source_ts

        # Note: Pane marker is ONLY touched on confirmed HTTP 200 delivery in Step C to avoid masking outages.

        # Canonical Lease Takeover Truth Table:
        # holder == os.getpid() or holder is None -> CLAIM
        # holder is dead -> CLAIM
        # holder is alive and now_wall < active_lease + 0.5s -> DEFER
        # holder is alive and now_wall >= active_lease + 0.5s -> CLAIM (hung takeover)
        active_lease = session_record.get("lease_deadline") or 0.0
        sending_pid = session_record.get("sending_pid")

        if sending_pid is not None and sending_pid != os.getpid():
            l_tok = session_record.get("lease_token") or ""
            l_parts = l_tok.split(":")
            holder_st = l_parts[1] if len(l_parts) >= 2 else None
            if is_process_instance_alive(sending_pid, holder_st):
                if now_wall < active_lease + 0.5:
                    touch_reconciler_pending()
                    ensure_reconciler_running()
                    cache_mgr.save(data)
                    return
                # Hung process (> deadline + 0.5s): claim and take over
            # Dead process or reused PID: claim and take over

        # Claim sending lease with unique lease token defeating PID reuse
        my_token = f"{os.getpid()}:{own_start_time()}:{now_wall}:{session_id}"
        session_record["lease_token"] = my_token
        session_record["sending_pid"] = os.getpid()
        session_record["lease_deadline"] = now_wall + 1.5
        my_resync_gen = session_record.get("resync_generation", 0)
        session_record["lease_resync_gen"] = my_resync_gen
        cache_mgr.save(data)
        current_payload = payload
        transmitting_state = mapped_state
        transmitting_seq = new_seq

    # 2. Outside lock: transmit and drain queue with lease ownership verification
    while current_payload:
        success, is_non_retryable = post_bartender_event(current_payload, bridge_url=bridge_url)
        vendor_to_clean = None
        orphans_to_export = []
        orphans_to_remove = []
        should_spawn_reconciler = False
        compensation_needed = False
        with cache_mgr as data:
            s = data.get("sessions", {}).get(session_id)
            is_tombstoned = bool(canonical_pane in data.get("tombstones", {}))
            if not s or (is_tombstoned and transmitting_state != "Ended"):
                if success and transmitting_state != "Ended":
                    log_debug(f"Stale send landed for evicted/closed session {session_id}; staging compensation")
                    compensation_needed = True
                    comp_entry = {
                        "session_id": session_id,
                        "agent": current_payload.get("agent", "Herdr") if current_payload else "Herdr",
                        "generation": s.get("generation", 1) if s else 1,
                        "admitted_at_ns": s.get("admitted_at_ns", 0) if s else 0,
                        "timestamp": clock.time(),
                    }
                    data.setdefault("pending_compensations", []).append(comp_entry)
                    cache_mgr.save(data)
                break
            if s.get("lease_token") != my_token:
                log_debug(f"Lease token superseded for {session_id}, forcing re-sync and exiting drain loop")
                s["delivered_seq"] = 0
                s["delivery_status"] = "in_flight"
                s["resync_generation"] = s.get("resync_generation", 0) + 1
                touch_reconciler_pending()
                should_spawn_reconciler = True
                cache_mgr.save(data)
                break

            if success:
                if s.get("resync_generation", 0) > my_resync_gen:
                    log_debug(f"Superseding sender landed during lease for {session_id}, forcing re-sync")
                    s["delivered_seq"] = 0
                    s["delivery_status"] = "in_flight"
                    touch_reconciler_pending()
                    should_spawn_reconciler = True
                    cache_mgr.save(data)
                    break

                s["delivered_state"] = transmitting_state
                s["delivered_seq"] = transmitting_seq
                s["delivery_status"] = "delivered"
                s["delivery_attempts"] = 0
                s.pop("delivery_error", None)
                data["last_successful_delivery"] = clock.time()
                data["consecutive_failures"] = 0
                touch_pane_marker(canonical_pane)
                clear_delivery_down()
                clear_pane_failed(canonical_pane)
                vendor_to_clean = canonical_pane
                data.setdefault("pending_vendor_cleanups", []).append({
                    "pane_id": canonical_pane,
                    "is_pane_closed": False,
                    "timestamp": clock.time(),
                })
                for other_s in data.get("sessions", {}).values():
                    if other_s.get("delivery_status") == "retryable_exhausted":
                        other_s["delivery_attempts"] = 0
                        other_s["delivery_status"] = "in_flight"

                # Active Supersession check on Ended
                if transmitting_state == "Ended" and s.get("seq") == transmitting_seq:
                    data.get("sessions", {}).pop(session_id, None)
                    remove_pane_marker(canonical_pane)
                    orphans_to_remove.append(session_id)
                    # Agent exit from shell does not create a container tombstone; pane survives.
                    if s.get("close_kind") == "agent_exit":
                        data.setdefault("agent_exits", {})[canonical_pane] = {
                            "exit_at_ns": s.get("exit_at_ns") or arr_ns,
                            "exit_source_ts": float(s.get("exit_source_ts") or event_data.get("timestamp") or 0.0),
                        }
                    elif s.get("close_kind") == "container":
                        data.setdefault("tombstones", {})[canonical_pane] = {
                            "closed_at_ns": s.get("closed_at_ns") or arr_ns,
                            "closed_source_ts": s.get("closed_source_ts", 0.0),
                            "last_source_timestamp": s.get("last_source_timestamp", 0.0),
                        }
                    cache_mgr.save(data)
                    break
            elif is_non_retryable:
                s["delivery_status"] = "non_retryable_failed"
                s["rejected_seq"] = transmitting_seq
                s["delivery_error"] = "bridge_rejected"
                touch_pane_failed(canonical_pane)
                if transmitting_state == "Ended":
                    s["orphaned_ended"] = True
                    orphans_to_export.append((session_id, dict(s)))
            else:
                s["delivery_attempts"] = s.get("delivery_attempts", 0) + 1
                s["delivery_error"] = "retryable network error"
                touch_pane_failed(canonical_pane)
                data["consecutive_failures"] = data.get("consecutive_failures", 0) + 1
                if data["consecutive_failures"] >= 3:
                    touch_delivery_down()
                if s.get("delivery_attempts", 0) >= 5:
                    s["delivery_status"] = "retryable_exhausted"
                    touch_pane_failed(canonical_pane)
                    if s.get("desired_state") == "Ended":
                        s["orphaned_ended"] = True
                        orphans_to_export.append((session_id, dict(s)))

            # Drain check: if a newer state was requested while in-flight, loop and transmit it
            if s.get("delivered_seq", 0) < s.get("seq", 0) and runtime.time_remaining() > 0.3 and s.get("delivery_status") not in ("non_retryable_failed", "retryable_exhausted"):
                s["lease_deadline"] = clock.time() + 1.5
                current_payload = s.get("desired_payload")
                transmitting_state = s.get("desired_state")
                transmitting_seq = s.get("seq")
                cache_mgr.save(data)
            else:
                s["sending_pid"] = None
                s["lease_token"] = None
                s["lease_deadline"] = None
                if s.get("delivered_seq", 0) < s.get("seq", 0) and s.get("delivery_status") != "non_retryable_failed":
                    should_spawn_reconciler = True
                cache_mgr.save(data)
                current_payload = None

        if compensation_needed:
            # Under lock re-verification: abort compensation if a live active session exists
            with cache_mgr as data:
                active_s = data.get("sessions", {}).get(session_id)
                if active_s and active_s.get("desired_state") != "Ended":
                    log_debug(f"Aborting stale send compensation for {session_id}: active session re-admitted with state {active_s.get('desired_state')}")
                    compensation_needed = False
                data["pending_compensations"] = [
                    c for c in data.get("pending_compensations", [])
                    if c.get("session_id") != session_id
                ]
                cache_mgr.save(data)

            if compensation_needed:
                if runtime.time_remaining() > 0.3:
                    _raw_post_event(
                        {"state": "Ended", "agent": current_payload.get("agent", "Herdr") if current_payload else "Herdr", "session_id": session_id},
                        timeout=0.2,
                        bridge_url=bridge_url
                    )
                    # Re-acquire lock to detect race if session was admitted while compensation was in flight
                    with cache_mgr as data:
                        active_s = data.get("sessions", {}).get(session_id)
                        if active_s and active_s.get("desired_state") != "Ended":
                            log_debug(f"Compensating Ended raced with new admission for {session_id}; forcing re-sync")
                            active_s["delivered_seq"] = 0
                            active_s["delivery_status"] = "in_flight"
                            touch_reconciler_pending()
                            should_spawn_reconciler = True
                            cache_mgr.save(data)
                else:
                    touch_reconciler_pending()
                    should_spawn_reconciler = True
        for o_sid, o_info in orphans_to_export:
            export_orphan_record(o_sid, o_info)
        for o_sid in orphans_to_remove:
            remove_orphan_record(o_sid)
        if should_spawn_reconciler:
            ensure_reconciler_running()
        if vendor_to_clean:
            if runtime.time_remaining() > 0.3:
                cleanup_vendor_active(vendor_to_clean, is_pane_closed=(transmitting_state == "Ended"), bridge_url=bridge_url)
                with cache_mgr as data:
                    data["pending_vendor_cleanups"] = [
                        cln for cln in data.get("pending_vendor_cleanups", [])
                        if cln.get("pane_id") != vendor_to_clean
                    ]
                    cache_mgr.save(data)
            else:
                touch_reconciler_pending()
                ensure_reconciler_running()
