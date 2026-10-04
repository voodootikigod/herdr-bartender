"""Process-wide mutable runtime state.

Every module reads and writes these through the module (``runtime.X``), never via
``from .runtime import X``, so the watchdog, the cache and the tests all see a
single copy.
"""

from __future__ import annotations

import signal
from typing import Optional, Tuple

from . import clock, paths

DEFAULT_DEADLINE_SECONDS = 1.5  # Plan §6.1 L698: ITIMER_REAL hard deadline

# Deadline modes (Plan §6.1). The ``time_remaining() - 0.3`` budget formulas bind only the
# watchdog-bounded event path; the reconciler, --cleanup and --replay-orphans are exempt
# from the 1.5s watchdog (Plan L619, L686) and use their callers' own timeouts.
DEADLINE_BOUNDED = "bounded"
DEADLINE_UNBOUNDED = "unbounded"
_DEADLINE_MODES = (None, DEADLINE_BOUNDED, DEADLINE_UNBOUNDED)

PROCESS_DEADLINE_SECONDS = DEFAULT_DEADLINE_SECONDS
START_TIME = clock.monotonic()
PROCESS_ARRIVAL_TIME = clock.time()
PROCESS_ARRIVAL_TIME_NS = clock.time_ns()
MODULE_LOAD_TIME = clock.time()
IN_CRITICAL_SECTION = False
# True inside watchdog.deferred_exit(): the event is not yet saved or spooled, so SIGALRM only sets
# PENDING_WATCHDOG_EXIT (like a critical section) and the exit happens when the section ends.
IN_DEFER_SECTION = False
PENDING_WATCHDOG_EXIT = False
# Resolved lazily by process.own_start_time(); never looked up at import.
PROCESS_START_TIME = None
# None: inferred by deadline_bounded(); otherwise DEADLINE_BOUNDED / DEADLINE_UNBOUNDED.
DEADLINE_MODE: Optional[str] = None


def mark_process_start(launched_at: Optional[Tuple[float, int]] = None) -> None:
    """Set the deadline origin and the arrival stamps (cli.main()'s first statement).

    ``launched_at`` is ``(time.monotonic(), time.time_ns())`` captured by
    bin/herdr-bartender before it imports the package, so package import (and any
    bytecode compilation) counts against PROCESS_DEADLINE_SECONDS as the Plan §6.1
    budget table requires ("process startup & module load"), and the arrival stamp
    is as close to process entry as Python can take it (Plan §4.3 step 1). Without
    it (in-process callers) the origin is now.

    It is also where the process takes the private umask 077 (Plan §8 L1089),
    before any state file or directory is created.
    """
    global START_TIME, PROCESS_ARRIVAL_TIME, PROCESS_ARRIVAL_TIME_NS, MODULE_LOAD_TIME
    paths.apply_private_umask()
    if launched_at is None:
        launched_at = (clock.monotonic(), clock.time_ns())
    START_TIME, PROCESS_ARRIVAL_TIME_NS = float(launched_at[0]), int(launched_at[1])
    PROCESS_ARRIVAL_TIME = PROCESS_ARRIVAL_TIME_NS / 1e9
    MODULE_LOAD_TIME = clock.time()


def raw_time_remaining() -> float:
    """Seconds left before PROCESS_DEADLINE_SECONDS (may be negative); arm_watchdog() uses it."""
    return PROCESS_DEADLINE_SECONDS - (clock.monotonic() - START_TIME)


def time_remaining() -> float:
    return max(0.1, raw_time_remaining())


def set_deadline_mode(mode: Optional[str]) -> None:
    """Declare this process bounded/unbounded (None: infer it). Entry points call this once."""
    global DEADLINE_MODE
    if mode not in _DEADLINE_MODES:
        raise ValueError(f"unknown deadline mode: {mode!r}")
    DEADLINE_MODE = mode


def _watchdog_armed() -> bool:
    """True while an ITIMER_REAL (the SIGALRM watchdog) is pending."""
    getitimer = getattr(signal, "getitimer", None)
    if getitimer is None or not hasattr(signal, "ITIMER_REAL"):
        return False
    try:
        return getitimer(signal.ITIMER_REAL)[0] > 0
    except (OSError, ValueError):
        return False


def deadline_bounded() -> bool:
    """True when this process must honour the Plan §6.1 1.5s budget.

    An explicit DEADLINE_MODE wins. Otherwise the process is bounded while the
    SIGALRM watchdog is armed (or has fired), and during its first
    PROCESS_DEADLINE_SECONDS (the event path's identity warm-up before
    arm_watchdog()). Past that window with no watchdog it is an unbounded mode
    (reconciler, --cleanup, --replay-orphans): time_remaining() is pinned at its
    floor there and must not shrink every timeout to the minimum.
    """
    if DEADLINE_MODE is not None:
        return DEADLINE_MODE == DEADLINE_BOUNDED
    if PENDING_WATCHDOG_EXIT or _watchdog_armed():
        return True
    return clock.monotonic() - START_TIME < PROCESS_DEADLINE_SECONDS


def budget_allows(reserve: float) -> bool:
    """Plan §6.1 gate ``time_remaining() > reserve``; always True outside the bounded path."""
    return not deadline_bounded() or time_remaining() > reserve


def snapshot() -> dict:
    """Capture the mutable runtime state (used by tests to restore it)."""
    return {
        name: globals()[name]
        for name in (
            "PROCESS_DEADLINE_SECONDS",
            "START_TIME",
            "PROCESS_ARRIVAL_TIME",
            "PROCESS_ARRIVAL_TIME_NS",
            "MODULE_LOAD_TIME",
            "IN_CRITICAL_SECTION",
            "IN_DEFER_SECTION",
            "PENDING_WATCHDOG_EXIT",
            "PROCESS_START_TIME",
            "DEADLINE_MODE",
        )
    }


def restore(state: dict) -> None:
    globals().update(state)
