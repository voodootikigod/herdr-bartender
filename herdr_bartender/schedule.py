"""Reconciler loop timing (Plan §5.1 items 3, 5, 11; R13): pure functions, no I/O.

* A full pass runs every 20s, or every 300s once Bartender has been absent for more
  than 1000s (wall clock); a ``reconciler.pending`` touch, a spool/results arrival or
  any due work (retry, dismissal, compensation, lease grace end, TTL/salvage/Herdr-dead
  expiry, R12 horizon) wakes it earlier (but never more often than every 0.5s).
* Two absence counters (R26): ``backoff_since`` drives the 300s backoff and is reset by
  Bartender's return AND by any wake-up (pending touch, envelope arrival: Plan §5.1 items
  3/11); ``absent_since`` is the 12h terminal-horizon origin and only Bartender's return
  resets it, so the reconciler's own wake-ups can never postpone the terminal horizon.
* The 20s marker heartbeat keeps its cadence during the 300s backoff (R13).
* Terminal horizon: Bartender absent for more than 43200s AND Herdr confirmed dead.
* Idle exit: 60 consecutive seconds with no session, no owed side effect, no queued
  envelope/journal/orphan work and a healthy bridge (health probe ok, no DELIVERY_DOWN).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, List, Mapping, Optional, Tuple

from .delivery_state import LEASE_GRACE_SECONDS, foreign_lease_open
from .dismissals import earliest_due as dismissal_due
from .lifecycle import (
    HERDR_DEAD_EXPIRY_SECONDS,
    ORPHAN_HORIZON_SECONDS,
    SALVAGE_HORIZON_SECONDS,
    TTL_SECONDS,
    horizon_start,
    is_live,
    number,
    retry_due_at,
    undelivered,
)
from .sender.compensation import next_compensation_wait
from .snapshot import Instance

ACTIVE_INTERVAL_SECONDS = 20.0
BACKOFF_INTERVAL_SECONDS = 300.0
HEARTBEAT_SECONDS = 20.0
ABSENCE_BACKOFF_AFTER_SECONDS = 1000.0
TERMINAL_HORIZON_SECONDS = 43200.0
IDLE_EXIT_SECONDS = 60.0
WAKE_FLOOR_SECONDS = 0.5
EXPIRY_SLACK_SECONDS = 1e-3   # expiries need age > TTL: wake just past the boundary


@dataclass(frozen=True)
class CacheView:
    """What the loop needs to know about the cache after a pass (read under the lock, then released)."""

    sessions: int = 0
    exhausted: int = 0
    heartbeat_panes: Tuple[str, ...] = ()
    owed: bool = False              # compensations, vendor cleanups or dismissals still queued
    next_due: Optional[float] = None


@dataclass(frozen=True)
class LoopState:
    absent_since: Optional[float] = None    # Bartender absence: the terminal-horizon origin
    backoff_since: Optional[float] = None   # the absence counter of the 300s backoff (reset by wake-ups too)
    idle_since: Optional[float] = None
    next_sweep_at: float = 0.0
    next_heartbeat_at: float = 0.0
    next_due: Optional[float] = None


# -- cache view ---------------------------------------------------------------------------
def heartbeat_eligible(record: Mapping) -> bool:
    """Plan §5.1 item 5: live, confirmed delivered, never salvaged."""
    return is_live(record) and record.get("delivery_status") == "delivered" and not record.get("salvaged")


def _upcoming(due: float, now: float) -> Optional[float]:
    """A wake-up for a one-shot boundary only while it lies ahead: once crossed, the pass that crossed it applied
    it (and a horizon export that failed is retried on the regular cadence), so a past boundary must not keep the
    next due time in the past (a full pass every 0.5s)."""
    return due if due > now else None


def _expiry_time(record: Mapping, herdr_alive: bool, now: float) -> Optional[float]:
    if not is_live(record):
        start = horizon_start(record)
        return None if start is None else _upcoming(start + ORPHAN_HORIZON_SECONDS + EXPIRY_SLACK_SECONDS, now)
    last = number(record.get("last_event_at"))
    if last is None:
        return now
    if record.get("salvaged"):
        return now if not herdr_alive else last + SALVAGE_HORIZON_SECONDS + EXPIRY_SLACK_SECONDS
    ttl = TTL_SECONDS.get(str(record.get("desired_state")))
    return None if ttl is None else last + ttl + EXPIRY_SLACK_SECONDS


def _send_time(record: Mapping, now: float) -> Optional[float]:
    if not undelivered(record):
        return None
    due = retry_due_at(record, now)
    if foreign_lease_open(record, now):
        due = max(due, float(record.get("lease_deadline") or now) + LEASE_GRACE_SECONDS)
    return due


def _session_times(records: Iterable[Mapping], herdr_alive: bool, now: float) -> List[float]:
    times: List[float] = []
    for record in records:
        for due in (_send_time(record, now), _expiry_time(record, herdr_alive, now)):
            if due is not None:
                times.append(due)
    return times


def _root_times(data: Mapping, now: float, herdr_alive: bool = True) -> List[float]:
    times: List[float] = []
    dead_since = number(data.get("herdr_dead_since"))
    dead_expiry = None if dead_since is None else _upcoming(dead_since + HERDR_DEAD_EXPIRY_SECONDS
                                                            + EXPIRY_SLACK_SECONDS, now)
    if dead_expiry is not None:
        times.append(dead_expiry)
    owed = next_compensation_wait(data.get("pending_compensations") or [], now)
    if owed is not None:
        times.append(now + owed)
    if data.get("pending_vendor_cleanups") and herdr_alive:   # R70: deferred while Herdr is dead
        times.append(now)
    dismissal = dismissal_due(data.get("dismissed_vendor_uuids"), now)
    if dismissal is not None:
        times.append(dismissal)
    return times


def cache_view(data: Mapping, now: float, herdr_alive: bool) -> CacheView:
    records = [r for r in (data.get("sessions") or {}).values() if isinstance(r, dict)]
    times = _session_times(records, herdr_alive, now) + _root_times(data, now, herdr_alive)
    owed = bool(data.get("pending_compensations") or (data.get("pending_vendor_cleanups") and herdr_alive)
                or data.get("dismissed_vendor_uuids"))
    return CacheView(
        sessions=len(records),
        exhausted=sum(1 for r in records if r.get("delivery_status") == "retryable_exhausted"),
        heartbeat_panes=tuple(r["pane_id"] for r in records if heartbeat_eligible(r) and r.get("pane_id")),
        owed=owed,
        next_due=min(times) if times else None,
    )


# -- presence, horizons, idle ------------------------------------------------------------------
def track_presence(state: LoopState, bartender: Instance, now: float) -> LoopState:
    """Wall-clock Bartender absence (Plan §5.1 item 11); an unknown probe changes nothing.

    Bartender coming back ends the backoff at once: the next full pass is due now (restart
    detection, re-sync and the exhausted sessions' recovery do not wait out a 300s sleep).
    """
    if bartender.pid is not None:
        if state.absent_since is not None or state.backoff_since is not None:
            return replace(state, absent_since=None, backoff_since=None, next_sweep_at=min(state.next_sweep_at, now))
        return state
    if bartender.absent and (state.absent_since is None or state.backoff_since is None):
        return replace(state, absent_since=now if state.absent_since is None else state.absent_since,
                       backoff_since=now if state.backoff_since is None else state.backoff_since)
    return state


def cancel_backoff(state: LoopState) -> LoopState:
    """Plan §5.1 items 3/11: a pending touch or an envelope arrival resets the backoff's absence counter.

    The terminal-horizon origin (``absent_since``) is kept: a wake-up says nothing about Bartender (R26).
    """
    return replace(state, backoff_since=None)


def _elapsed(since: Optional[float], now: float) -> float:
    return 0.0 if since is None else max(0.0, now - since)


def absent_for(state: LoopState, now: float) -> float:
    """How long Bartender has been confirmed absent (the terminal horizon)."""
    return _elapsed(state.absent_since, now)


def sweep_interval(state: LoopState, now: float) -> float:
    backoff = _elapsed(state.backoff_since, now) > ABSENCE_BACKOFF_AFTER_SECONDS
    return BACKOFF_INTERVAL_SECONDS if backoff else ACTIVE_INTERVAL_SECONDS


def terminal_horizon_reached(state: LoopState, herdr: Instance, now: float) -> bool:
    return absent_for(state, now) > TERMINAL_HORIZON_SECONDS and herdr.absent


def track_idle(state: LoopState, idle_now: bool, now: float) -> LoopState:
    if not idle_now:
        return replace(state, idle_since=None)
    return state if state.idle_since is not None else replace(state, idle_since=now)


def idle_expired(state: LoopState, now: float) -> bool:
    return state.idle_since is not None and now - state.idle_since >= IDLE_EXIT_SECONDS


# -- wake-ups ----------------------------------------------------------------------------
def after_pass(state: LoopState, now: float, full: bool, next_due: Optional[float]) -> LoopState:
    """Timers after a pass: a full pass resets the sweep timer (a shorter interval also pulls a pending
    one in, e.g. once the absence backoff ended); every pass ran the heartbeat."""
    interval_end = now + sweep_interval(state, now)
    sweep_at = interval_end if full else min(state.next_sweep_at, interval_end)
    return replace(state, next_sweep_at=sweep_at, next_heartbeat_at=now + HEARTBEAT_SECONDS, next_due=next_due)


def full_pass_due(state: LoopState, now: float) -> bool:
    if now >= state.next_sweep_at:
        return True
    return state.next_due is not None and now >= state.next_due


def next_wake(state: LoopState, now: float) -> float:
    """Wall time the loop sleeps until (it still wakes early on pending/spool/results arrival)."""
    wakes = [state.next_sweep_at, state.next_heartbeat_at]
    if state.next_due is not None:
        wakes.append(max(now + WAKE_FLOOR_SECONDS, state.next_due))
    if state.idle_since is not None:
        wakes.append(state.idle_since + IDLE_EXIT_SECONDS)
    return max(now + WAKE_FLOOR_SECONDS, min(wakes))
