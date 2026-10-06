"""Session lifecycle the reconciler applies under the cache lock (Plan §5.1 items 4, 7-9, 12; §3.3 TTL row).

Pure functions over the cache ``data`` and a pre-lock ``snapshot.ProcessSnapshot``: no
subprocess, no network and no orphan-file I/O happen here (gap lock-discipline-subprocess).

* TTLs (§3.3): Working 12h, Waiting 48h, Idle/Done 24h after ``last_event_at`` (strictly
  greater); a salvaged record after 300s, or at once when Herdr is dead (§6.3).
* Herdr dead for more than 300s (``herdr_dead_since`` persisted in the cache) expires EVERY
  live session, salvaged ones included; a Herdr restart (R14) expires every live session of
  the OLD instance - one whose last activity predates the new instance's start time (all of
  them when that start time is unknown) - so sessions the new instance already admitted
  through the event path survive the reconciler's late restart detection.
* An expiry stages a real Ended (gaps herdr-restart-stale-payload, ttl-herdr-dead):
  ``desired_state``/``desired_payload`` Ended, ``seq + 1``, ``in_flight``, attempts reset,
  ``salvaged = False``, ``ttl_expired_at`` stamped. It is sent by the Universal Sender like
  any other seq; the session is evicted only on a confirmed 200.
* A Bartender restart (R14) or a reconnection after DELIVERY_DOWN is a Full Top Shelf
  Re-Sync (§5.1 item 4): ``delivered_seq = 0`` for every live, non-salvaged session.
* R12 horizon: an Ended still unconfirmed 12h after its TTL expiry (``ttl_expired_at``) or
  after it was orphaned (``orphaned_at``: rejected, or retries exhausted) is exported to the
  orphan file and evicted (the caller does the export outside the lock, then the eviction).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Tuple

from .cache import ORPHAN_MIRROR_OWED, mirror_copy
from .delivery_state import RETRY_DELAYS, foreign_lease_open, resync_live_sessions
from .log import log_debug
from .process import known_start_time, start_times_differ
from .snapshot import BARTENDER, HERDR, Instance, ProcessSnapshot

TTL_SECONDS = {"Working": 43200.0, "Waiting": 172800.0, "Idle": 86400.0, "Done": 86400.0}
SALVAGE_HORIZON_SECONDS = 300.0
HERDR_DEAD_EXPIRY_SECONDS = 300.0
ORPHAN_HORIZON_SECONDS = 43200.0     # R12: 12h past the TTL / the orphaning

UNSENDABLE_STATUSES = ("non_retryable_failed", "retryable_exhausted", "salvaged")
DUE_TOLERANCE_SECONDS = 1e-3   # float rounding of wall-clock stamps must not push a due retry past its tick
MAX_RETRY_DELAY_SECONDS = max(RETRY_DELAYS)   # a retry stamped further ahead than this: the wall clock stepped back

REASON_TTL = "ttl"
REASON_SALVAGE = "salvage_horizon"
REASON_HERDR_DEAD = "herdr_dead"
REASON_HERDR_RESTART = "herdr_restart"


@dataclass(frozen=True)
class LifecycleReport:
    expired: Tuple[str, ...] = ()
    resynced: Tuple[str, ...] = ()
    bartender_restarted: bool = False
    herdr_restarted: bool = False


def number(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _sessions(data: dict) -> Iterable[Tuple[str, dict]]:
    return [(sid, rec) for sid, rec in data.get("sessions", {}).items() if isinstance(rec, dict)]


def is_live(record: Mapping) -> bool:
    return record.get("desired_state") != "Ended"


# -- payloads ---------------------------------------------------------------------------
def ended_payload(sid: str, record: Mapping, seq: int) -> dict:
    return {"state": "Ended", "agent": record.get("agent") or "Herdr", "session_id": sid, "seq": seq}


def state_payload(sid: str, record: Mapping) -> dict:
    """The transmit payload for ``record``'s desired state (built when none consistent is stored)."""
    seq = int(record.get("seq", 0) or 0)
    state = record.get("desired_state") or "Working"
    if state == "Ended":
        return ended_payload(sid, record, seq)
    return {"state": state, "agent": record.get("agent") or "Herdr", "session_id": sid,
            "title": record.get("title") or f"Session {record.get('pane_id') or 'unknown'}",
            "terminal": "Herdr", "seq": seq}


def ensure_payload(sid: str, record: dict) -> bool:
    """Make ``desired_payload`` transmit ``desired_state`` for ``sid``; True when it had to be rebuilt."""
    payload = record.get("desired_payload")
    if isinstance(payload, dict) and payload.get("state") == record.get("desired_state") \
            and payload.get("session_id") == sid:
        return False
    record["desired_payload"] = state_payload(sid, record)
    return True


def retry_due_at(record: Mapping, now: float) -> float:
    """When the record's next retry is due (now when unscheduled).

    ``next_retry_at`` is a wall-clock stamp at most ``MAX_RETRY_DELAY_SECONDS`` ahead of the time it was
    written; one further in the future means the wall clock stepped back since, and the retry is due now
    instead of waiting out the size of the step (as compensations and dismissals already do).
    """
    retry_at = number(record.get("next_retry_at"))
    if retry_at is None or retry_at > now + MAX_RETRY_DELAY_SECONDS + DUE_TOLERANCE_SECONDS:
        return now
    return retry_at


def sendable(record: Mapping, now: float) -> bool:
    """An undelivered seq the reconciler may send now (retry schedule honoured, terminal statuses excluded)."""
    if record.get("salvaged") or record.get("delivery_status") in UNSENDABLE_STATUSES:
        return False
    if int(record.get("delivered_seq", 0) or 0) >= int(record.get("seq", 0) or 0):
        return False
    return retry_due_at(record, now) <= now + DUE_TOLERANCE_SECONDS


def undelivered(record: Mapping) -> bool:
    """A seq not yet confirmed and not excluded from retries (it keeps the reconciler busy)."""
    return not record.get("salvaged") and record.get("delivery_status") not in UNSENDABLE_STATUSES \
        and int(record.get("delivered_seq", 0) or 0) < int(record.get("seq", 0) or 0)


# -- expiry ---------------------------------------------------------------------------
def expire_session(sid: str, record: dict, now: float, reason: str, horizon: bool = True) -> None:
    """Stage an Ended for ``record`` (a real Ended payload, next seq); sent and evicted like any close.

    ``horizon`` stamps ``ttl_expired_at`` (the R12 12h horizon origin) unless already stamped.
    """
    seq = int(record.get("seq", 0) or 0) + 1
    record.update({
        "desired_state": "Ended", "seq": seq, "desired_payload": ended_payload(sid, record, seq),
        "salvaged": False, "delivery_status": "in_flight", "delivery_error": None, "delivery_attempts": 0,
        "next_retry_at": None, "expiry_reason": reason,
    })
    if horizon and number(record.get("ttl_expired_at")) is None:
        record["ttl_expired_at"] = now
    log_debug(f"Expiring {sid} to Ended ({reason})")


def ttl_reason(record: Mapping, now: float, herdr_alive: bool) -> Optional[str]:
    """Why ``record`` must expire now, or None (Plan §3.3 TTL row, §6.3 salvage horizon)."""
    if not is_live(record):
        return None
    age = now - (number(record.get("last_event_at")) or now)
    if record.get("salvaged"):
        return REASON_SALVAGE if (not herdr_alive or age > SALVAGE_HORIZON_SECONDS) else None
    ttl = TTL_SECONDS.get(str(record.get("desired_state")))
    return REASON_TTL if ttl is not None and age > ttl else None


def expire_stale(data: dict, now: float, herdr_alive: bool) -> Tuple[str, ...]:
    """Apply the TTLs; a record without ``last_event_at`` - or stamped in the future, i.e. before a backward
    wall-clock step - starts aging now (a step back never extends a TTL by its size)."""
    expired = []
    for sid, record in _sessions(data):
        last = number(record.get("last_event_at"))
        if last is None or last > now:
            record["last_event_at"] = now
        reason = ttl_reason(record, now, herdr_alive)
        if reason:
            expire_session(sid, record, now, reason)
            expired.append(sid)
    return tuple(expired)


def expire_all(data: dict, now: float, reason: str) -> Tuple[str, ...]:
    """Herdr dead >5m or restarted: every live session (salvaged included) to Ended."""
    expired = []
    for sid, record in _sessions(data):
        if is_live(record):
            expire_session(sid, record, now, reason)
            expired.append(sid)
    return tuple(expired)


def _started_at(start_time: object) -> Optional[float]:
    text = known_start_time(start_time)
    try:
        return float(text) if text is not None else None
    except ValueError:
        return None


def _last_activity(record: Mapping) -> Optional[float]:
    """Wall time of the session's latest event or admission (None when unknown)."""
    admitted = number(record.get("admitted_at_ns"))
    stamps = [t for t in (number(record.get("last_event_at")), None if admitted is None else admitted / 1e9)
              if t is not None]
    return max(stamps) if stamps else None


def expire_old_instance(data: dict, now: float, new_start: object) -> Tuple[str, ...]:
    """Herdr restarted (R14, Plan §5.1 item 9): expire the live sessions of the old instance.

    A session whose last activity is at or after the new instance's start was admitted by the new
    instance (the event path can run before the reconciler notices the restart) and is kept. With an
    unknown start time nothing tells the instances apart and every live session expires.
    """
    started = _started_at(new_start)
    expired = []
    for sid, record in _sessions(data):
        if not is_live(record):
            continue
        last = _last_activity(record)
        if started is not None and last is not None and last >= started:
            continue
        expire_session(sid, record, now, REASON_HERDR_RESTART)
        expired.append(sid)
    return tuple(expired)


# -- restarts (R14) --------------------------------------------------------------------
def instance_restarted(last_pid: object, last_start: object, current: Instance, old_gone: Optional[bool]) -> bool:
    """R14: a PID change whose old instance is confirmed gone, or a start-time change on the same PID."""
    if not isinstance(last_pid, int) or current.pid is None:
        return False
    if current.pid != last_pid:
        return old_gone is True
    return start_times_differ(last_start, current.start_time)


def _record_instance(data: dict, kind: str, current: Instance) -> None:
    data[f"last_{kind}_pid"] = current.pid
    if current.start_time is not None:
        data[f"last_{kind}_start_time"] = current.start_time
    if kind == HERDR:
        data["herdr_instance_id"] = f"{current.pid}:{current.start_time or ''}"


def track_instance(data: dict, kind: str, snap: ProcessSnapshot) -> bool:
    """Detect a restart of ``kind`` and record the current instance; True on a restart."""
    current = snap.instance(kind)
    last_pid, last_start = data.get(f"last_{kind}_pid"), data.get(f"last_{kind}_start_time")
    restarted = instance_restarted(last_pid, last_start, current, snap.old_gone(kind, last_pid))
    if current.pid is not None and (last_pid is None or restarted or last_pid == current.pid):
        _record_instance(data, kind, current)
    return restarted


def full_resync(data: dict) -> Tuple[str, ...]:
    """Plan §5.1 item 4: re-assert every live, non-salvaged session (salvaged ones stay quiescent)."""
    return resync_live_sessions(data)


def track_herdr_dead(data: dict, snap: ProcessSnapshot, now: float) -> bool:
    """Persist ``herdr_dead_since``; True once Herdr has been confirmed dead for more than 300s."""
    if snap.herdr.pid is not None:
        data["herdr_dead_since"] = None
        return False
    if not snap.herdr.absent:
        return False  # failed probe: no information
    since = number(data.get("herdr_dead_since"))
    if since is None or since > now:
        data["herdr_dead_since"] = now
        return False
    return now - since > HERDR_DEAD_EXPIRY_SECONDS


def apply_lifecycle(data: dict, snap: ProcessSnapshot, now: float) -> LifecycleReport:
    """Restart detection, Herdr-dead tracking and TTLs, in that order (one critical section)."""
    bartender_restarted = track_instance(data, BARTENDER, snap)
    herdr_restarted = track_instance(data, HERDR, snap)
    if bartender_restarted:
        log_debug("Bartender restarted (old instance confirmed gone); Full Top Shelf Re-Sync")
    herdr_dead_too_long = track_herdr_dead(data, snap, now)
    expired: Tuple[str, ...] = ()
    if herdr_restarted:
        log_debug("Herdr instance restarted (old instance confirmed gone); expiring the old instance's sessions")
        expired = expire_old_instance(data, now, snap.herdr.start_time)
    elif herdr_dead_too_long:
        log_debug("Herdr dead for more than 5 minutes; expiring every session")
        expired = expire_all(data, now, REASON_HERDR_DEAD)
    expired += expire_stale(data, now, snap.herdr_alive)
    resynced = full_resync(data) if bartender_restarted else ()
    return LifecycleReport(expired, resynced, bartender_restarted, herdr_restarted)


# -- R12 horizon ----------------------------------------------------------------------------
def horizon_start(record: Mapping) -> Optional[float]:
    """When the 12h orphan horizon of an unconfirmed Ended started (TTL expiry or orphaning), else None."""
    if is_live(record):
        return None
    starts = [t for t in (number(record.get("ttl_expired_at")), number(record.get("orphaned_at"))) if t is not None]
    return min(starts) if starts else None


def past_horizon(record: Mapping, now: float) -> bool:
    start = horizon_start(record)
    return start is not None and now - start > ORPHAN_HORIZON_SECONDS and not foreign_lease_open(record, now)


def horizon_exports(data: dict, now: float) -> Tuple[Tuple[str, dict], ...]:
    """(sid, record copy) for every unconfirmed Ended past the R12 horizon."""
    return tuple((sid, dict(record)) for sid, record in _sessions(data) if past_horizon(record, now))


def mirror_owed_records(data: dict) -> Tuple[Tuple[str, dict], ...]:
    """(sid, record copy) for every record flagged ``ORPHAN_MIRROR_OWED`` (R52: its orphan export is not durable)."""
    return tuple((sid, dict(record)) for sid, record in _sessions(data) if record.get(ORPHAN_MIRROR_OWED) is True)


def all_sessions(data: dict) -> Tuple[Tuple[str, dict], ...]:
    """(sid, record copy) for every cached session (the terminal absence horizon exports them all)."""
    return tuple((sid, dict(record)) for sid, record in _sessions(data))


def orphan_record(record: Mapping) -> dict:
    """The orphan-file copy of an unconfirmed session: replayed as an Ended (Plan §9.2)."""
    return mirror_copy(dict(record), desired_state="Ended", orphaned_ended=True)


def same_record(current: Optional[Mapping], exported: Mapping) -> bool:
    """The session did not move on since it was exported (same seq and desired state)."""
    return isinstance(current, dict) and current.get("seq") == exported.get("seq") \
        and current.get("desired_state") == exported.get("desired_state")


def evict_exported(data: dict, exported: Iterable[Tuple[str, dict]]) -> Tuple[Tuple[str, Optional[str]], ...]:
    """Pop the exported sessions that are unchanged; returns (sid, pane) of the evicted ones."""
    sessions = data.get("sessions", {})
    evicted = []
    for sid, record in exported:
        if same_record(sessions.get(sid), record):
            evicted.append((sid, sessions.pop(sid).get("pane_id")))
    return tuple(evicted)
