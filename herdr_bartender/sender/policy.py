"""Budget / deadline policy for the Universal Sender (Plan §4.3 Step B, §5.2, §6.1; R5, R9, R10).

Every network gate, socket timeout, lock wait, orphan-lock mode and reconciler hand-off
the sender performs is decided by a ``SendPolicy``:

* ``EVENT_POLICY`` - the watchdog-bounded plugin process (1.5s deadline): every POST is
  gated on ``time_remaining() > 0.3`` with a socket timeout of
  ``min(0.2, max(0.05, time_remaining() - 0.3))``; at most one session and two POSTs
  (primary + minimal Ended retry) per process (R5); the orphan lock is a 50ms LOCK_NB
  (R10); unfinished work is handed to the detached reconciler.
* ``BACKGROUND_POLICY`` - the reconciler (Plan L619: exempt from the 1.5s watchdog). No
  event-path budget gate, a fixed 0.2s socket timeout, unbounded session count, a
  blocking orphan lock and the longer reconciler lock wait. It never spawns itself:
  hand-off only touches ``reconciler.pending`` so the loop runs another pass. The
  reconciler runs inside ``runtime.deadline_mode(DEADLINE_UNBOUNDED)`` so the
  bridge/cache/process helpers stop applying the event-path formulas.
* ``cleanup_policy(n)`` - ``--cleanup`` (Plan §5.2): exempt from the 1.5s watchdog, bypasses
  DISABLED, a 0.15s socket timeout per session and one overall budget of
  ``max(10.0, n * 0.15)`` seconds (``deadline``, on the injectable monotonic clock).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .. import clock, handoff, runtime
from ..cache import UNBOUNDED_LOCK_TIMEOUT, BoundedSessionCache

NETWORK_RESERVE_SECONDS = 0.3   # Plan §6.1: no network I/O with 0.3s or less left
SOCKET_CAP_SECONDS = 0.2        # Plan §5.2: < 200ms per POST
SOCKET_FLOOR_SECONDS = 0.05
CLEANUP_SOCKET_SECONDS = 0.15   # Plan §5.2: --cleanup per-session timeout
CLEANUP_BASE_BUDGET = 10.0      # Plan §5.2: budget = max(10.0, len(sessions) * 0.15)
CLEANUP_PER_SESSION = 0.15


@dataclass(frozen=True)
class SendPolicy:
    name: str
    bounded: bool                         # True: the 1.5s watchdog-bounded event path
    max_sessions: Optional[int] = 1       # R5: sessions delivered per call (None: unbounded)
    max_posts: int = 2                    # R5: primary POST + one minimal Ended retry
    reserve: float = NETWORK_RESERVE_SECONDS
    socket_cap: float = SOCKET_CAP_SECONDS
    orphan_blocking: bool = False         # R10: event path LOCK_NB 50ms, reconciler blocks
    lock_timeout: Optional[float] = None  # None: the cache default (bounded R9 formula)
    spawns_reconciler: bool = True        # False: the caller IS the reconciler (or must not start one)
    honors_disabled: bool = True          # False: --cleanup runs under DISABLED (Plan §5.2)
    deadline: Optional[float] = None      # clock.monotonic() budget end of an unbounded caller (--cleanup)

    def allows_network(self) -> bool:
        """Plan §6.1 gate: ``time_remaining() > 0.3`` on the bounded path; else only the caller's own budget."""
        if self.deadline is not None and clock.monotonic() >= self.deadline:
            return False
        return not self.bounded or runtime.time_remaining() > self.reserve

    def socket_timeout(self) -> float:
        """``min(0.2, max(0.05, time_remaining() - 0.3))`` on the bounded path, else the fixed cap."""
        if not self.bounded:
            return self.socket_cap
        return min(self.socket_cap, max(SOCKET_FLOOR_SECONDS, runtime.time_remaining() - self.reserve))

    def cache(self, state_dir: Path) -> BoundedSessionCache:
        """The session cache as this policy locks it (DISABLED re-checked under the lock unless exempt)."""
        return BoundedSessionCache(state_dir, lock_timeout=self.lock_timeout, check_disabled=self.honors_disabled)

    def hand_off(self) -> None:
        """Unfinished work: flag ``reconciler.pending`` and (event path) make sure a reconciler runs."""
        handoff.touch_reconciler_pending()
        if self.spawns_reconciler:
            handoff.ensure_reconciler_running()

    def ensure_watchdog(self) -> None:
        """Plan §4.3 L568 unconditional watchdog check: a live session needs the reconciler's heartbeat."""
        if self.spawns_reconciler:
            handoff.ensure_reconciler_running()


EVENT_POLICY = SendPolicy("event", bounded=True)
BACKGROUND_POLICY = SendPolicy("background", bounded=False, max_sessions=None, orphan_blocking=True,
                               lock_timeout=UNBOUNDED_LOCK_TIMEOUT, spawns_reconciler=False)


def cleanup_budget(session_count: int) -> float:
    """Plan §5.2: ``max(10.0, len(sessions) * 0.15)`` seconds."""
    return max(CLEANUP_BASE_BUDGET, session_count * CLEANUP_PER_SESSION)


def cleanup_policy(session_count: int, deadline: Optional[float] = None) -> SendPolicy:
    """--cleanup: unbounded mode, DISABLED bypassed, 0.15s per POST, one overall budget.

    ``deadline`` (R69): the run's own budget end, fixed when --cleanup started; default: the budget from now.
    """
    end = deadline if deadline is not None else clock.monotonic() + cleanup_budget(session_count)
    return SendPolicy("cleanup", bounded=False, max_sessions=None, socket_cap=CLEANUP_SOCKET_SECONDS,
                      orphan_blocking=True, lock_timeout=UNBOUNDED_LOCK_TIMEOUT, spawns_reconciler=False,
                      honors_disabled=False, deadline=end)
