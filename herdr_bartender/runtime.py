"""Process-wide mutable runtime state.

Every module reads and writes these through the module (``runtime.X``), never via
``from .runtime import X``, so the watchdog, the cache and the tests all see a
single copy.
"""

from __future__ import annotations

from . import clock

DEFAULT_DEADLINE_SECONDS = 1.4

PROCESS_DEADLINE_SECONDS = DEFAULT_DEADLINE_SECONDS
START_TIME = clock.monotonic()
PROCESS_ARRIVAL_TIME = clock.time()
PROCESS_ARRIVAL_TIME_NS = clock.time_ns()
MODULE_LOAD_TIME = clock.time()
IN_CRITICAL_SECTION = False
PENDING_WATCHDOG_EXIT = False
# Resolved lazily by process.own_start_time(); never looked up at import.
PROCESS_START_TIME = None


def mark_process_start() -> None:
    """Re-baseline the deadline clock and arrival stamps once dispatch is ready.

    The original single-file script took these right after compiling itself, so
    module compilation never counted against PROCESS_DEADLINE_SECONDS. With a
    package, runtime is imported before ~20 sibling modules (which may need
    compiling when bytecode cannot be cached); cli.main() calls this first so the
    budget is measured from the same point as before.
    """
    global START_TIME, PROCESS_ARRIVAL_TIME, PROCESS_ARRIVAL_TIME_NS, MODULE_LOAD_TIME
    START_TIME = clock.monotonic()
    PROCESS_ARRIVAL_TIME = clock.time()
    PROCESS_ARRIVAL_TIME_NS = clock.time_ns()
    MODULE_LOAD_TIME = clock.time()


def time_remaining() -> float:
    elapsed = clock.monotonic() - START_TIME
    rem = PROCESS_DEADLINE_SECONDS - elapsed
    return max(0.1, rem)


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
            "PENDING_WATCHDOG_EXIT",
            "PROCESS_START_TIME",
        )
    }


def restore(state: dict) -> None:
    globals().update(state)
