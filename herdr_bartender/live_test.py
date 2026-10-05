"""--live-test (alias --test): live acceptance check against the REAL Bartender bridge (Plan §10.2 item 1).

Run it only by hand, with Bartender 6 running and Top Shelf enabled. It never touches the
plugin's session cache, markers or spool: it talks to the bridge directly with a session id
of its own (``herdr:<host>:hb-livetest:<random>``), so no real Herdr pane can collide with it.

1. ``GET /health`` must answer ``ok: true`` with an integer ``sessions`` count: the baseline.
2. ``POST /event`` Working, Waiting, Done, Idle, each of which must return HTTP 200 ``{"ok": true}``
   (paused ``STEP_PAUSE_SECONDS`` apart so a person can watch Top Shelf change).
3. ``POST /event`` Ended for the same session id, sent even when an earlier step failed so the
   test entry never strands on Top Shelf. It must return ``{"ok": true}`` too.
4. ``/health`` is polled for up to ``SETTLE_SECONDS`` until ``sessions`` is back at the baseline.

Every check is printed as ``[PASS]``/``[FAIL]``; the run ends with ``RESULT: PASS`` (exit 0) or
``RESULT: FAIL`` (exit 1). Each POST is sent once (no minimal-payload retry), so a rejection
is reported instead of being papered over.
"""

from __future__ import annotations

import sys
import uuid
from typing import Callable, List, Optional, TextIO, Tuple

from . import clock, runtime
from .bridge import check_bridge_health, send_event
from .config import get_bridge_url, get_sanitized_hostname

LIVE_STATES = ("Working", "Waiting", "Done", "Idle")
LIVE_AGENT = "Claude (Herdr)"
LIVE_TERMINAL = "Herdr"
LIVE_PANE_PREFIX = "hb-livetest"
STEP_PAUSE_SECONDS = 1.0
SETTLE_SECONDS = 3.0
SETTLE_POLL_SECONDS = 0.25
LIVE_TIMEOUT = 2.0   # per request; this is an interactive command, not the 1.5s event path

STEP_TITLES = {
    "Working": "herdr-bartender live test: Working (spinner)",
    "Waiting": "herdr-bartender live test: Waiting (attention)",
    "Done": "herdr-bartender live test: Done",
    "Idle": "herdr-bartender live test: Idle",
}

Check = Tuple[bool, str]


def live_session_id(host: Optional[str] = None) -> str:
    """A session id unique to this run; it matches SESSION_ID_REGEX and no Herdr pane id."""
    return f"herdr:{host or get_sanitized_hostname()}:{LIVE_PANE_PREFIX}:{uuid.uuid4().hex[:16]}"


def _health_sessions(health: Optional[dict]) -> Optional[int]:
    """The integer ``sessions`` count of a healthy /health answer, else None."""
    if not isinstance(health, dict) or health.get("ok") is not True:
        return None
    count = health.get("sessions")
    if isinstance(count, bool) or not isinstance(count, int):
        return None
    return count


def _post(payload: dict) -> Check:
    result = send_event(payload, timeout=LIVE_TIMEOUT)
    label = f"POST {payload['state']}"
    if result.success:
        return True, f"{label} -> ok:true"
    return False, f"{label} -> {result.error} (HTTP {result.http_status})"


def _status_payload(state: str, session_id: str) -> dict:
    return {"state": state, "agent": LIVE_AGENT, "session_id": session_id,
            "title": STEP_TITLES[state], "terminal": LIVE_TERMINAL}


def _wait_for_baseline(baseline: int, settle: float) -> Check:
    """Poll /health until ``sessions`` equals ``baseline`` or ``settle`` seconds pass."""
    deadline = clock.monotonic() + settle
    seen: Optional[int] = None
    while True:
        seen = _health_sessions(check_bridge_health(timeout=LIVE_TIMEOUT))
        if seen == baseline:
            return True, f"/health sessions back to baseline {baseline} after Ended"
        if clock.monotonic() >= deadline:
            return False, f"/health sessions {seen!r} did not return to baseline {baseline} within {settle:.1f}s"
        clock.sleep(SETTLE_POLL_SECONDS)


def _run_steps(session_id: str, baseline: int, pause: float, settle: float,
               emit: Callable[[Check], None]) -> None:
    try:
        for state in LIVE_STATES:
            ok, msg = _post(_status_payload(state, session_id))
            emit((ok, msg))
            if not ok:
                break
            clock.sleep(pause)
    finally:
        ended = _post({"state": "Ended", "agent": LIVE_AGENT, "session_id": session_id})
        emit(ended)
    if ended[0]:
        emit(_wait_for_baseline(baseline, settle))


def _report(checks: List[Check], out: TextIO) -> int:
    passed = bool(checks) and all(ok for ok, _ in checks)
    print(f"\nRESULT: {'PASS' if passed else 'FAIL'} ({sum(ok for ok, _ in checks)}/{len(checks)} checks)", file=out)
    return 0 if passed else 1


def run_live_test(out: Optional[TextIO] = None, pause: float = STEP_PAUSE_SECONDS,
                  settle: float = SETTLE_SECONDS) -> int:
    """Run the live acceptance check and print a PASS/FAIL report; returns the exit code (0/1)."""
    out = out or sys.stdout
    checks: List[Check] = []

    def emit(check: Check) -> None:
        checks.append(check)
        print(f"[{'PASS' if check[0] else 'FAIL'}] {check[1]}", file=out, flush=True)

    with runtime.deadline_mode(runtime.DEADLINE_UNBOUNDED):
        print(f"herdr-bartender live test against {get_bridge_url()}", file=out)
        baseline = _health_sessions(check_bridge_health(timeout=LIVE_TIMEOUT))
        if baseline is None:
            emit((False, "/health unreachable, not ok, or without an integer sessions count "
                         "(is Bartender 6 running with Top Shelf enabled?)"))
            return _report(checks, out)
        emit((True, f"/health ok, baseline sessions = {baseline}"))
        session_id = live_session_id()
        print(f"test session_id: {session_id}", file=out)
        _run_steps(session_id, baseline, pause, settle, emit)
    return _report(checks, out)
