"""--cleanup: end every tracked session on shutdown/rollback.

--cleanup is exempt from the 1.5s event deadline (Plan L619/L686, R10): it declares
the unbounded deadline mode, so the cache lock waits with the longer budget. A
confirmation that cannot be recorded under the lock is written to results/ for
the reconciler (Plan §4.3 L483) rather than lost.
"""

from __future__ import annotations

from . import clock, runtime
from .bridge import post_bartender_event
from .cache import BoundedSessionCache, CacheError
from .delivery_state import STATUS_NON_RETRYABLE, STATUS_RETRYABLE, STATUS_SUCCESS, Outcome, Transmission
from .log import log_debug
from .markers import remove_pane_marker, touch_heartbeat
from .orphans import export_orphan_record, remove_orphan_record
from .paths import get_state_dir
from .results import defer_result


def _outcome(success: bool, is_non_retryable: bool) -> Outcome:
    if success:
        return Outcome(STATUS_SUCCESS)
    return Outcome(STATUS_NON_RETRYABLE if is_non_retryable else STATUS_RETRYABLE)


def _record_result(cache_mgr: BoundedSessionCache, sid: str, pane_id, target_seq: int, success: bool,
                   now: float) -> bool:
    """Apply one cleanup POST under the lock; False when a cached session was not cleared. Raises CacheError."""
    cleared = True
    with cache_mgr as data:
        s = data.get("sessions", {}).get(sid)
        if s:
            if success:
                if s.get("seq") == target_seq:
                    data.get("sessions", {}).pop(sid, None)
                    remove_pane_marker(pane_id)
                    remove_orphan_record(sid)
            else:
                cleared = False
                s["delivery_attempts"] = s.get("delivery_attempts", 0) + 1
                s["last_attempt"] = now
        cache_mgr.save(data)
    return cleared


def run_cleanup(bridge_url: str | None = None) -> int:
    """Cleans up all active sessions on shutdown/rollback.
    Returns:
      0: All sessions confirmed Ended via bridge (HTTP 200).
      2: Bridge unreachable; pending sessions exported to $HOME/.herdr-bartender-orphans.json (mode 0600).
      1: Fatal error during cleanup.
    """
    runtime.set_deadline_mode(runtime.DEADLINE_UNBOUNDED)
    touch_heartbeat()
    state_dir = get_state_dir()
    cache_mgr = BoundedSessionCache(state_dir)
    now = clock.time()
    all_cleared = True
    target_sessions = []
    try:
        with cache_mgr as data:
            sessions = data.get("sessions", {})
            for session_id, info in list(sessions.items()):
                seq = int(info.get("seq", 0)) + 1
                info["seq"] = seq
                info["desired_state"] = "Ended"
                target_sessions.append((session_id, info.get("pane_id"), info.get("agent", "Herdr"), seq))
            cache_mgr.save(data)

        for sid, pane_id, agent_name, target_seq in target_sessions:
            payload = {"state": "Ended", "agent": agent_name, "session_id": sid}
            success, is_non_retryable = post_bartender_event(payload, timeout=0.2, bridge_url=bridge_url)
            try:
                cleared = _record_result(cache_mgr, sid, pane_id, target_seq, success, now)
            except CacheError as exc:  # the confirmation goes to results/ instead of being lost
                defer_result(Transmission(sid, pane_id, "Ended", target_seq, agent_name or "Herdr"),
                             _outcome(success, is_non_retryable), exc)
                cleared = success
            all_cleared = all_cleared and cleared

        if not all_cleared:
            try:
                with cache_mgr as data:
                    for sid, s in data.get("sessions", {}).items():
                        if s.get("delivered_state") != "Ended":
                            export_orphan_record(sid, s)
            except Exception:
                pass
            return 2

        return 0
    except Exception as e:
        log_debug(f"Fatal error during cleanup: {e}")
        return 1
