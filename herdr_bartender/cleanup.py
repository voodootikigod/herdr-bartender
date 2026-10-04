"""--cleanup: end every tracked session on shutdown/rollback."""

from __future__ import annotations

from . import clock
from .bridge import post_bartender_event
from .cache import BoundedSessionCache
from .log import log_debug
from .markers import remove_pane_marker, touch_heartbeat
from .orphans import export_orphan_record, remove_orphan_record
from .paths import get_state_dir


def run_cleanup(bridge_url: str | None = None) -> int:
    """Cleans up all active sessions on shutdown/rollback.
    Returns:
      0: All sessions confirmed Ended via bridge (HTTP 200).
      2: Bridge unreachable; pending sessions exported to $HOME/.herdr-bartender-orphans.json (mode 0600).
      1: Fatal error during cleanup.
    """
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
            with cache_mgr as data:
                s = data.get("sessions", {}).get(sid)
                if s:
                    if success:
                        if s.get("seq") == target_seq:
                            data.get("sessions", {}).pop(sid, None)
                            remove_pane_marker(pane_id)
                            remove_orphan_record(sid)
                    else:
                        all_cleared = False
                        s["delivery_attempts"] = s.get("delivery_attempts", 0) + 1
                        s["last_attempt"] = now
                cache_mgr.save(data)

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
