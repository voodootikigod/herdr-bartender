"""Single reconcile pass over the session cache (Step A/B/C for the reconciler)."""

from __future__ import annotations

import os
from pathlib import Path

from . import clock
from .bridge import _raw_post_event, post_bartender_event
from .cache import BoundedSessionCache
from .log import log_debug
from .markers import (
    clear_delivery_down,
    clear_pane_failed,
    is_disabled,
    remove_pane_marker,
    touch_delivery_down,
    touch_pane_failed,
    touch_pane_marker,
)
from .orphans import export_orphan_record, orphan_pane_ids, remove_orphan_record
from .process import (
    get_bartender_pid,
    get_herdr_pid,
    get_process_start_time,
    is_herdr_alive,
    is_pid_alive,
    is_process_instance_alive,
    own_start_time,
)
from .handoff import touch_reconciler_pending
from .sender import BACKGROUND_POLICY, compensate
from .sender.compensation import valid_entry as valid_compensation
from .vendor import cleanup_vendor_active


def _owed_compensations(cache_mgr: BoundedSessionCache) -> list:
    """Snapshot of the well-formed owed compensations; malformed entries are purged (they could never be sent)."""
    with cache_mgr as data:
        entries = data.get("pending_compensations", [])
        valid = [c for c in entries if valid_compensation(c)]
        if len(valid) != len(entries):
            log_debug(f"Dropping {len(entries) - len(valid)} malformed pending_compensations entries")
            data["pending_compensations"] = valid
            cache_mgr.save(data)
    return valid


def drain_compensations(cache_mgr: BoundedSessionCache, bridge_url: str | None = None) -> bool:
    """Drain persisted compensations through the Universal Sender; True when ``reconciler.pending`` was flagged.

    Each entry gets the under-lock re-verification (target generation, retry schedule), its attempt recorded, the
    POST, then clear + re-sync detection; it is cleared only by an abort, a landed POST or its export to the orphan
    file once its attempts are exhausted. A forced re-sync (a re-admission an Ended may have dismissed) flags another
    pass. An owed entry is NOT re-flagged: the loop sleeps until it is due (background._compensation_wait).
    """
    need = False
    for comp in _owed_compensations(cache_mgr):
        need = compensate(cache_mgr, comp, BACKGROUND_POLICY, bridge_url) or need
    if need:
        touch_reconciler_pending()
    return need


def reconcile_active_sessions(state_dir: Path, bridge_url: str | None = None):
    now_wall = clock.time()
    # Warm the lease-token start time outside any cache-lock critical section.
    own_start_time()
    cache_mgr = BoundedSessionCache(state_dir)

    drain_compensations(cache_mgr, bridge_url)

    # Drain persisted vendor cleanups
    with cache_mgr as data:
        pending_cleanups = list(data.get("pending_vendor_cleanups", []))
    for cln in pending_cleanups:
        c_pane = cln.get("pane_id")
        cleanup_vendor_active(c_pane, is_pane_closed=cln.get("is_pane_closed", False), bridge_url=bridge_url)
        with cache_mgr as data:
            data["pending_vendor_cleanups"] = [
                c for c in data.get("pending_vendor_cleanups", [])
                if c.get("pane_id") != c_pane
            ]
            cache_mgr.save(data)

    # Drain / re-send dismissed vendor UUIDs with fallback cancellation
    to_retry_dismissals = []
    with cache_mgr as data:
        d_uuids = data.get("dismissed_vendor_uuids", {})
        panes_dir = state_dir / "panes"
        for v_uuid, meta in list(d_uuids.items()):
            pane_hex = meta.get("pane_hex", "")
            ts = meta.get("timestamp", 0.0)
            va_file = panes_dir / f"{pane_hex}.vendor_active"
            failed_file = panes_dir / f"{pane_hex}.failed"
            cancel_dismissal = (
                va_file.exists() or
                failed_file.exists() or
                not is_herdr_alive() or
                is_disabled() or
                (state_dir / "NO_HOOKS").exists() or
                (now_wall - ts > 60)
            )
            if cancel_dismissal or (now_wall - ts > 60):
                d_uuids.pop(v_uuid, None)
                continue

            attempts = meta.get("attempts", 1)
            last_attempt = meta.get("last_attempt", ts)
            if now_wall - last_attempt >= 2.0 and attempts < 5:
                to_retry_dismissals.append((v_uuid, dict(meta)))

            if now_wall - ts >= 10.0 or attempts >= 5:
                d_uuids.pop(v_uuid, None)
        cache_mgr.save(data)

    for v_uuid, meta in to_retry_dismissals:
        _raw_post_event({"state": "Ended", "session_id": v_uuid, "agent": "Vendor"}, timeout=0.2, bridge_url=bridge_url)
        now_post = clock.time()
        with cache_mgr as data:
            d_uuids = data.get("dismissed_vendor_uuids", {})
            if v_uuid in d_uuids:
                curr_meta = d_uuids[v_uuid]
                new_attempts = curr_meta.get("attempts", 1) + 1
                curr_meta["attempts"] = new_attempts
                curr_meta["last_attempt"] = now_post
                if now_post - curr_meta.get("timestamp", 0.0) >= 10.0 or new_attempts >= 5:
                    d_uuids.pop(v_uuid, None)
                cache_mgr.save(data)

    targets = []
    orphan_panes = orphan_pane_ids()  # read outside the cache lock (gap save-reads-orphans-under-lock)
    with cache_mgr as data:
        sessions = data.get("sessions", {})
        if not sessions:
            return

        # Bartender PID restart detection (earliest-start, dead-PID/start-time debounced) -> trigger Full Re-Sync
        curr_bartender_pid = get_bartender_pid()
        curr_bartender_st = get_process_start_time(curr_bartender_pid) if curr_bartender_pid else ""
        last_bartender_pid = data.get("last_bartender_pid")
        last_bartender_st = data.get("last_bartender_start_time")
        is_old_bartender_dead = False
        if last_bartender_pid is not None:
            if not is_pid_alive(last_bartender_pid) or (last_bartender_st and get_process_start_time(last_bartender_pid) != last_bartender_st):
                is_old_bartender_dead = True
        if curr_bartender_pid is not None and last_bartender_pid is not None and (curr_bartender_pid != last_bartender_pid or (last_bartender_st and curr_bartender_st != last_bartender_st)) and is_old_bartender_dead:
            log_debug(f"Bartender restarted (PID {last_bartender_pid} confirmed dead/restarted -> new PID {curr_bartender_pid}); triggering Full Re-Sync")
            for s in sessions.values():
                if s.get("desired_state") != "Ended" and not s.get("salvaged", False):
                    s["delivered_seq"] = 0
                    s["delivery_status"] = "in_flight"
        if curr_bartender_pid is not None:
            data["last_bartender_pid"] = curr_bartender_pid
            if curr_bartender_st:
                data["last_bartender_start_time"] = curr_bartender_st

        # Herdr instance restart detection -> sweep old sessions
        curr_herdr_pid = get_herdr_pid()
        curr_herdr_st = get_process_start_time(curr_herdr_pid) if curr_herdr_pid else ""
        last_herdr_pid = data.get("last_herdr_pid")
        last_herdr_st = data.get("last_herdr_start_time")
        is_old_dead = False
        if last_herdr_pid is not None:
            if not is_pid_alive(last_herdr_pid) or (last_herdr_st and get_process_start_time(last_herdr_pid) != last_herdr_st):
                is_old_dead = True
        if last_herdr_pid is not None and (curr_herdr_pid != last_herdr_pid or (last_herdr_st and curr_herdr_st != last_herdr_st)) and is_old_dead and curr_herdr_pid is not None:
            log_debug(f"Herdr instance restart detected (PID {last_herdr_pid} confirmed dead/restarted -> new PID {curr_herdr_pid}); marking old sessions Ended")
            for sid, s in list(sessions.items()):
                if s.get("desired_state") != "Ended":
                    s["desired_state"] = "Ended"
                    s["seq"] = int(s.get("seq", 0)) + 1
            data["last_herdr_pid"] = curr_herdr_pid
            data["herdr_instance_id"] = f"{curr_herdr_pid}:{curr_herdr_st or ''}"
            if curr_herdr_st:
                data["last_herdr_start_time"] = curr_herdr_st
        elif last_herdr_pid is None and curr_herdr_pid is not None:
            data["last_herdr_pid"] = curr_herdr_pid
            data["herdr_instance_id"] = f"{curr_herdr_pid}:{curr_herdr_st or ''}"
            if curr_herdr_st:
                data["last_herdr_start_time"] = curr_herdr_st

        for sid, s in list(sessions.items()):
            pane_id = s.get("pane_id")

            # Check for stale session (> 24 hours for Idle/Done/Salvaged, > 48 hours for Waiting)
            is_stale = False
            last_ts = s.get("last_event_at", now_wall)
            if s.get("salvaged", False) and (now_wall - last_ts > 300 or not is_herdr_alive()):
                is_stale = True
            elif s.get("desired_state") in ("Idle", "Done") and (now_wall - last_ts > 86400):
                is_stale = True
            elif s.get("desired_state") == "Waiting" and (now_wall - last_ts > 172800):
                is_stale = True
            elif s.get("desired_state") == "Working" and (now_wall - last_ts > 43200):
                is_stale = True

            # Hard horizon: if stale, exhausted, and undelivered > 12h past TTL, export to orphans and evict
            if is_stale and s.get("delivery_status") == "retryable_exhausted" and (now_wall - last_ts > 129600):
                export_orphan_record(sid, s)
                sessions.pop(sid, None)
                remove_pane_marker(pane_id)
                continue

            if is_stale:
                s["desired_state"] = "Ended"
                s["salvaged"] = False
                s["seq"] = int(s.get("seq", 0)) + 1
                s["desired_payload"] = {
                    "state": "Ended",
                    "agent": s.get("agent", "Herdr"),
                    "session_id": sid,
                    "seq": s["seq"],
                }

            # Quiescent salvaged sessions are excluded from transmission until live event or TTL expiry
            if s.get("salvaged", False):
                continue

            if s.get("delivered_seq", 0) < s.get("seq", 0) or s.get("desired_state") == "Ended":
                if s.get("desired_state") == "Ended" and s.get("delivery_attempts", 0) >= 5:
                    s["orphaned_ended"] = True
                    continue
                if s.get("delivery_status") in ("non_retryable_failed", "retryable_exhausted"):
                    continue

                lease = s.get("lease_deadline") or 0
                sending_pid = s.get("sending_pid")
                l_tok = s.get("lease_token") or ""
                l_parts = l_tok.split(":")
                holder_st = l_parts[1] if len(l_parts) >= 2 else None
                if lease and (now_wall < lease) and (sending_pid != os.getpid()) and is_process_instance_alive(sending_pid, holder_st):
                    continue

                payload = s.get("desired_payload")
                if not payload:
                    payload = {
                        "state": s.get("desired_state", "Working"),
                        "agent": s.get("agent", "Herdr"),
                        "session_id": sid,
                        "title": s.get("title", f"Session {pane_id or 'unknown'}"),
                        "terminal": "Herdr",
                        "seq": s.get("seq", 1),
                    }
                my_token = f"{os.getpid()}:{own_start_time()}:{now_wall}:{sid}"
                s["lease_token"] = my_token
                s["sending_pid"] = os.getpid()
                s["lease_deadline"] = now_wall + 1.5
                my_resync_gen = s.get("resync_generation", 0)
                targets.append((sid, s.get("desired_state"), s.get("seq", 1), pane_id, payload, my_token, my_resync_gen))
        cache_mgr.save(data, orphan_panes=orphan_panes)

    for sid, target_state, target_seq, pane_id, payload, my_token, my_resync_gen in targets:
        with cache_mgr as data:
            s = data.get("sessions", {}).get(sid)
            if not s or s.get("lease_token") != my_token:
                continue
        success, is_non_retryable = post_bartender_event(payload, timeout=0.2, bridge_url=bridge_url)
        vendor_to_clean = None
        orphans_to_export = []
        orphans_to_remove = []
        compensation_needed = False
        with cache_mgr as data:
            s = data.get("sessions", {}).get(sid)
            is_tombstoned = bool(pane_id and pane_id in data.get("tombstones", {}))
            if not s or (is_tombstoned and target_state != "Ended"):
                if success and target_state != "Ended":
                    log_debug(f"Stale reconcile send landed for evicted/closed session {sid}; staging compensation")
                    compensation_needed = True
                continue
            if s.get("lease_token") != my_token:
                log_debug(f"Lease token superseded for {sid} during reconcile, forcing re-sync")
                s["resync_generation"] = s.get("resync_generation", 0) + 1
                s["delivered_seq"] = 0
                s["delivery_status"] = "in_flight"
                touch_reconciler_pending()
                cache_mgr.save(data)
                continue
            s["sending_pid"] = None
            s["lease_token"] = None
            s["lease_deadline"] = None
            if success:
                if s.get("resync_generation", 0) > my_resync_gen:
                    log_debug(f"Resync generation superseded for {sid} during reconcile, forcing re-sync")
                    s["delivered_seq"] = 0
                    s["delivery_status"] = "in_flight"
                    touch_reconciler_pending()
                else:
                    s["delivered_state"] = target_state
                    s["delivered_seq"] = target_seq
                    s["delivery_attempts"] = 0
                    s["delivery_status"] = "delivered"
                    s.pop("delivery_error", None)
                    data["last_successful_delivery"] = clock.time()
                    data["consecutive_failures"] = 0
                    touch_pane_marker(pane_id)
                    clear_delivery_down()
                    clear_pane_failed(pane_id)
                    vendor_to_clean = pane_id
                    for other_s in data.get("sessions", {}).values():
                        if other_s.get("delivery_status") == "retryable_exhausted":
                            other_s["delivery_attempts"] = 0
                            other_s["delivery_status"] = "in_flight"
                    if target_state == "Ended" and s.get("seq") == target_seq:
                        data.get("sessions", {}).pop(sid, None)
                        remove_pane_marker(pane_id)
                        orphans_to_remove.append(sid)
                        if pane_id and s.get("close_kind") == "container":
                            data.setdefault("tombstones", {})[pane_id] = {
                                "closed_at_ns": s.get("closed_at_ns") or clock.time_ns(),
                                "closed_source_ts": s.get("closed_source_ts", 0.0),
                                "last_source_timestamp": s.get("last_source_timestamp", 0.0),
                            }
            elif is_non_retryable:
                s["delivery_status"] = "non_retryable_failed"
                s["rejected_seq"] = target_seq
                s["delivery_error"] = "bridge_rejected"
                touch_pane_failed(pane_id)
                if target_state == "Ended":
                    s["orphaned_ended"] = True
                    orphans_to_export.append((sid, dict(s)))
            else:
                s["delivery_attempts"] = s.get("delivery_attempts", 0) + 1
                s["delivery_error"] = "retryable network error"
                touch_pane_failed(pane_id)
                data["consecutive_failures"] = data.get("consecutive_failures", 0) + 1
                if data["consecutive_failures"] >= 3:
                    touch_delivery_down()
                if s.get("delivery_attempts", 0) >= 5:
                    s["delivery_status"] = "retryable_exhausted"
                    touch_pane_failed(pane_id)
                    if s.get("desired_state") == "Ended":
                        s["orphaned_ended"] = True
                        orphans_to_export.append((sid, dict(s)))
            cache_mgr.save(data)

        if compensation_needed:
            _raw_post_event(
                {"state": "Ended", "agent": payload.get("agent", "Herdr"), "session_id": sid},
                timeout=0.2,
                bridge_url=bridge_url
            )
        for o_sid, o_info in orphans_to_export:
            export_orphan_record(o_sid, o_info)
        for o_sid in orphans_to_remove:
            remove_orphan_record(o_sid)
        if vendor_to_clean:
            cleanup_vendor_active(vendor_to_clean, is_pane_closed=(target_state == "Ended"), bridge_url=bridge_url)


def retry_undelivered_sessions(cache_mgr: BoundedSessionCache, bridge_url: str | None = None):
    reconcile_active_sessions(cache_mgr.state_dir, bridge_url=bridge_url)
