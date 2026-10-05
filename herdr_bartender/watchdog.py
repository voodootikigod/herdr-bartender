"""SIGALRM process-deadline watchdog (Plan §6.1, R23).

The handler performs no I/O. It sets ``runtime.PENDING_WATCHDOG_EXIT`` and:

* inside a critical section (``runtime.IN_CRITICAL_SECTION``) it returns, so the
  lock holder finishes its atomic swap; the cache's lock release then calls
  ``honor_pending_exit()``;
* inside a deferred-exit section (``deferred_exit()``: an event handler from entry
  until its event is saved by Step A or written to the spool, and a Step C lock
  wait until its result is applied or written to results/) it also returns; the
  exit is honoured when the section ends, so the Plan §4.3 contention rule ("on
  lock failure the event is spooled") holds even when the deadline expires
  during the lock wait or the envelope write;
* outside both it raises ``WatchdogExpired`` (a BaseException, so ordinary
  ``except Exception`` blocks cannot swallow it) to unwind the main flow, even out
  of a blocking socket read. ``run_bounded()`` at the top of the event path catches
  it and does the reconciler hand-off.

All I/O (touching ``reconciler.pending``, spawning the reconciler, logging) runs
after the handler has returned, never inside it.
"""

from __future__ import annotations

import signal
from contextlib import contextmanager
from typing import Callable, Iterator, TypeVar

from . import runtime
from .log import log_debug
from . import handoff

MIN_ARM_SECONDS = 0.01
WATCHDOG_EXIT_CODE = 0

T = TypeVar("T")


class WatchdogExpired(BaseException):
    """Raised by the SIGALRM handler outside critical sections to unwind to ``run_bounded``."""


def _exit_deferred() -> bool:
    return runtime.IN_CRITICAL_SECTION or runtime.IN_DEFER_SECTION


def _timeout_watchdog(signum, frame) -> None:
    runtime.PENDING_WATCHDOG_EXIT = True
    if _exit_deferred():
        return
    raise WatchdogExpired()


def arm_watchdog() -> bool:
    """Arm ITIMER_REAL so it fires PROCESS_DEADLINE_SECONDS (1.5s) after the process start baseline."""
    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        log_debug("SIGALRM/setitimer unavailable; process deadline not enforced")
        return False
    seconds = max(MIN_ARM_SECONDS, runtime.raw_time_remaining())
    try:
        signal.signal(signal.SIGALRM, _timeout_watchdog)
        signal.setitimer(signal.ITIMER_REAL, seconds)
    except (OSError, ValueError) as e:  # ValueError: not the main thread
        log_debug(f"Could not arm the process watchdog: {e}")
        return False
    return True


def disarm_watchdog() -> None:
    if hasattr(signal, "setitimer"):
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
        except (OSError, ValueError) as e:
            log_debug(f"Could not disarm the process watchdog: {e}")


def hand_off_to_reconciler() -> None:
    """Non-blocking hand-off of any unfinished work: flag the reconciler and make sure it runs."""
    handoff.hand_off_to_reconciler()


def honor_pending_exit() -> None:
    """Exit 0 (after the hand-off) when a deadline passed; a no-op otherwise or while still locked.

    Called on every cache-lock release, at the end of a deferred-exit section, and
    usable as a main-flow checkpoint.
    """
    if not runtime.PENDING_WATCHDOG_EXIT or _exit_deferred():
        return
    log_debug("Watchdog deadline passed; exiting after the critical section released the lock")
    hand_off_to_reconciler()
    raise SystemExit(WATCHDOG_EXIT_CODE)


@contextmanager
def deferred_exit() -> Iterator[None]:
    """Defer a SIGALRM exit until the block ends (R23 + Plan §4.3 contention rule).

    Wrap work that must not be cut short: taking the cache lock and then either
    saving or spooling the event, or applying / persisting a delivery result. Inside
    it the handler only sets the flag. Leaving the block normally then honours a
    passed deadline (reconciler hand-off, exit 0); an exception propagates unchanged.
    """
    outer = runtime.IN_DEFER_SECTION
    runtime.IN_DEFER_SECTION = True
    try:
        yield
    finally:
        runtime.IN_DEFER_SECTION = outer
    honor_pending_exit()


def run_bounded(fn: Callable[..., T], *args) -> T:
    """Run the event path; a SIGALRM outside critical sections becomes a hand-off and exit code 0.

    The timer is disarmed however the path ends, so a process that finishes just before the
    deadline is not killed by SIGALRM during its teardown (exit by signal 14 instead of 0).
    An alarm landing while it is disarmed is still caught here.
    """
    try:
        try:
            return fn(*args)
        finally:
            disarm_watchdog()
    except WatchdogExpired:
        log_debug("Watchdog deadline exceeded (SIGALRM); handing off to the reconciler")
        hand_off_to_reconciler()
        return WATCHDOG_EXIT_CODE  # type: ignore[return-value]
