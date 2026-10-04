"""SIGALRM process-deadline watchdog."""

from __future__ import annotations

import json
import signal
import sys

from . import runtime
from .log import log_debug
from .paths import get_state_dir
from .sender import ensure_reconciler_running


def _timeout_watchdog(signum, frame):
    if runtime.IN_CRITICAL_SECTION:
        runtime.PENDING_WATCHDOG_EXIT = True
        return
    log_debug("Watchdog deadline exceeded (SIGALRM)")
    # Non-blocking handoff to background helper before exit
    try:
        cache_file = get_state_dir() / "active-sessions.json"
        if cache_file.exists():
            with open(cache_file, "r") as f:
                d = json.load(f)
                for s in d.get("sessions", {}).values():
                    if s.get("delivered_seq", 0) < s.get("seq", 0) or s.get("desired_state") == "Ended":
                        # Shared hand-off: same detached spawn, plus the unit-testing guard.
                        ensure_reconciler_running()
                        break
    except Exception:
        pass
    sys.exit(0)


def arm_watchdog():
    if hasattr(signal, "SIGALRM"):
        try:
            signal.signal(signal.SIGALRM, _timeout_watchdog)
            if hasattr(signal, "setitimer"):
                signal.setitimer(signal.ITIMER_REAL, runtime.PROCESS_DEADLINE_SECONDS)
        except Exception:
            pass
