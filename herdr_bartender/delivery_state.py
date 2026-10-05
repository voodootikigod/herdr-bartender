"""Shared Step C apply-result logic (Plan §3.3 response matrix, §4.3 Step C, §5.1 item 1a).

``apply_delivery_result(data, tx, outcome)`` is THE single implementation of what a
delivery outcome does to the cache. Every sender's Step C (status, close, cascade,
reconciler sweep) and the results-directory drain call it under the cache lock, so a
confirmation applied late from ``results/`` has exactly the effects it would have had
in Step C.

It only mutates ``data`` and touches local pane marker files (no network, no orphan
file, no process spawns). Work that must happen outside the lock is returned as
``StagedEffects``; compensations and vendor cleanups are also persisted in
``data["pending_compensations"]`` / ``data["pending_vendor_cleanups"]`` (Plan §4.3
"Persisted Side Effects Guarantee") so a crash after the save cannot lose them.

The DELIVERY_DOWN flag is never touched here: its transition is staged as
``StagedEffects.delivery_down`` and the caller applies it with ``commit_delivery_down()``
right after ``cache_mgr.save()`` succeeds, still under the lock. A reconnection's Full Re-Sync
that is not saved therefore never loses the flag that makes the next confirmation re-sync.
"""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass, replace
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from . import clock
from .cache import ORPHAN_MIRROR_OWED, mirror_copy
from .log import log_debug, log_warning
from .markers import (
    clear_delivery_down,
    clear_pane_failed,
    is_delivery_down,
    remove_pane_marker,
    touch_delivery_down,
    touch_pane_failed,
    touch_pane_marker,
)
from .orphans import flush_pending_orphan_ops, journal_orphan_exports, run_orphan_io

STATUS_SUCCESS = "success"
STATUS_NON_RETRYABLE = "non_retryable"
STATUS_RETRYABLE = "retryable"
RESULT_STATUSES = (STATUS_SUCCESS, STATUS_NON_RETRYABLE, STATUS_RETRYABLE)

MAX_DELIVERY_ATTEMPTS = 5
# Plan §3.3 / §5.1 item 3: attempt n+1 is due RETRY_DELAYS[n] seconds after attempt n (0, 1, 2, 4, 8s; ~15s total).
RETRY_DELAYS = (0.0, 1.0, 2.0, 4.0, 8.0)
DELIVERY_DOWN_THRESHOLD = 3
LEASE_SECONDS = 1.5
LEASE_GRACE_SECONDS = 0.5   # Plan §1 L71-79: a holder past its deadline keeps the lease 0.5s more
DEFAULT_NON_RETRYABLE_ERROR = "bridge_rejected"
DEFAULT_RETRYABLE_ERROR = "network_timeout"

# verdicts
DELIVERED = "delivered"
EVICTED = "evicted"
RESYNC = "resync"
SUPERSEDED = "superseded"
STALE = "stale"
REJECTED = "rejected"
RETRY = "retry"
EXHAUSTED = "exhausted"
MISSING = "missing"
COMPENSATE = "compensate"
COMPENSATION_ABORTED = "compensation_aborted"
TOMBSTONED = "tombstoned"


@dataclass(frozen=True)
class Transmission:
    """Snapshot of what one sender transmitted (taken under the lease in Step B)."""

    session_id: str
    pane_id: Optional[str]
    state: str
    seq: int
    agent: str = "Herdr"
    lease_token: Optional[str] = None   # None: do not verify the lease (caller holds none)
    resync_generation: int = 0
    generation: Optional[int] = None
    admitted_at_ns: Optional[int] = None
    arrival_ns: Optional[int] = None    # fallback origin when a session lacks persisted close origins

    @classmethod
    def snapshot(cls, session_id: str, session: dict, lease_token: Optional[str],
                 arrival_ns: Optional[int] = None) -> "Transmission":
        payload = session.get("desired_payload") or {}
        return cls(
            session_id=session_id,
            pane_id=session.get("pane_id"),
            state=session.get("desired_state") or payload.get("state") or "",
            seq=int(session.get("seq", 0) or 0),
            agent=payload.get("agent") or session.get("agent") or "Herdr",
            lease_token=lease_token,
            resync_generation=int(session.get("resync_generation", 0) or 0),
            generation=session.get("generation"),
            admitted_at_ns=session.get("admitted_at_ns"),
            arrival_ns=arrival_ns,
        )

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "Transmission":
        known = {name: raw[name] for name in cls.__dataclass_fields__ if name in raw}
        return cls(**known)


@dataclass(frozen=True)
class Outcome:
    """Classified delivery outcome: ``status`` is success | non_retryable | retryable."""

    status: str
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.status == STATUS_SUCCESS

    @classmethod
    def from_delivery(cls, result) -> "Outcome":
        """From a ``bridge.DeliveryResult`` (same outcome vocabulary)."""
        return cls(result.outcome, result.error)


@dataclass(frozen=True)
class StagedEffects:
    """Post-lock work produced by one or more applied results."""

    verdict: str
    orphans_to_export: Tuple[Tuple[str, dict], ...] = ()
    orphans_to_remove: Tuple[str, ...] = ()
    vendor_cleanups: Tuple[dict, ...] = ()
    compensations: Tuple[dict, ...] = ()
    touch_pending: bool = False
    spawn_reconciler: bool = False
    evicted: bool = False
    followup: bool = False    # newer seq still undelivered and the caller kept the lease
    delivery_down: Optional[bool] = None   # DELIVERY_DOWN owed once saved: True set, False clear, None unchanged

    def merge(self, other: "StagedEffects") -> "StagedEffects":
        return StagedEffects(
            verdict=other.verdict,
            orphans_to_export=self.orphans_to_export + other.orphans_to_export,
            orphans_to_remove=self.orphans_to_remove + other.orphans_to_remove,
            vendor_cleanups=self.vendor_cleanups + other.vendor_cleanups,
            compensations=self.compensations + other.compensations,
            touch_pending=self.touch_pending or other.touch_pending,
            spawn_reconciler=self.spawn_reconciler or other.spawn_reconciler,
            evicted=self.evicted or other.evicted,
            followup=self.followup or other.followup,
            delivery_down=self.delivery_down if other.delivery_down is None else other.delivery_down,
        )


def foreign_lease_open(record: Mapping, now_wall: float) -> bool:
    """Another process holds ``record``'s lease and its deadline (+ grace) has not passed.

    Deadline only: whether the holder is still alive is the caller's concern
    (``sender.lease.lease_active_elsewhere`` checks it from the pre-lock memo).
    """
    sending_pid = record.get("sending_pid")
    if sending_pid is None or sending_pid == os.getpid():
        return False
    deadline = record.get("lease_deadline") or 0.0
    return isinstance(deadline, (int, float)) and now_wall < deadline + LEASE_GRACE_SECONDS


# -- persisted side effects ----------------------------------------------------------
def _same_session(entry: object, session_id: str) -> bool:
    return isinstance(entry, dict) and entry.get("session_id") == session_id


def stage_compensation(data: dict, tx: Transmission, now: float) -> dict:
    """Persist a compensating-Ended record for ``tx`` (one per session id) and return it.

    A replaced entry whose Ended may already have reached Bartender passes its ``posted``
    mark on, so a later re-admission is still re-synced (``sender.compensation``).
    """
    generation = tx.generation if tx.generation is not None else data.get("pane_generations", {}).get(tx.pane_id)
    entry = {
        "session_id": tx.session_id,
        "pane_id": tx.pane_id,
        "agent": tx.agent or "Herdr",
        "generation": generation,
        "admitted_at_ns": tx.admitted_at_ns or 0,
        "timestamp": now,
    }
    entries = data.get("pending_compensations", [])
    replaced = [c for c in entries if _same_session(c, tx.session_id)]
    if any(c.get("posted") for c in replaced):
        entry["posted"] = True
    data["pending_compensations"] = [c for c in entries if not _same_session(c, tx.session_id)] + [entry]
    return entry


def stage_vendor_cleanup(data: dict, pane_id: str, is_pane_closed: bool, now: float) -> dict:
    """Persist a ``cleanup_vendor_active`` request (one per pane; a close request wins)."""
    existing = [c for c in data.get("pending_vendor_cleanups", []) if c.get("pane_id") == pane_id]
    closed = is_pane_closed or any(c.get("is_pane_closed") for c in existing)
    entry = {"pane_id": pane_id, "is_pane_closed": closed, "timestamp": now}
    others = [c for c in data.get("pending_vendor_cleanups", []) if c.get("pane_id") != pane_id]
    data["pending_vendor_cleanups"] = others + [entry]
    return entry


def _num(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value or 0.0)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def record_tombstone(data: dict, pane: str, closed_at_ns: int, closed_source_ts: float,
                     last_source_timestamp: float) -> dict:
    """Record a pane tombstone; an existing one keeps its (earlier) origin and the stricter source bounds (R7)."""
    current = data.setdefault("tombstones", {}).get(pane)
    if isinstance(current, dict) and current.get("closed_at_ns"):
        closed_at_ns = min(int(current["closed_at_ns"]), closed_at_ns)
        closed_source_ts = max(_num(current.get("closed_source_ts")), closed_source_ts)
        last_source_timestamp = max(_num(current.get("last_source_timestamp")), last_source_timestamp)
    entry = {"closed_at_ns": closed_at_ns, "closed_source_ts": closed_source_ts,
             "last_source_timestamp": last_source_timestamp}
    data["tombstones"][pane] = entry
    return entry


def clear_pending_compensation(data: dict, session_id: str) -> None:
    data["pending_compensations"] = [c for c in data.get("pending_compensations", [])
                                     if c.get("session_id") != session_id]


def clear_pending_vendor_cleanup(data: dict, pane_id: str) -> None:
    data["pending_vendor_cleanups"] = [c for c in data.get("pending_vendor_cleanups", [])
                                       if c.get("pane_id") != pane_id]


# -- branch helpers --------------------------------------------------------------------
def _owns_lease(session: dict, tx: Transmission) -> bool:
    return tx.lease_token is not None and session.get("lease_token") == tx.lease_token


def _clear_lease(session: dict) -> None:
    session.update({"lease_token": None, "lease_deadline": None, "sending_pid": None})


def _force_resync(session: dict) -> None:
    session["delivered_seq"] = 0
    session["delivery_status"] = "in_flight"


def _may_have_landed(outcome: Outcome) -> bool:
    """Plan §4.3 Step C compensation: only a bridge rejection proves the send did not land.

    A retryable outcome (socket timeout, 5xx) may still have reached Bartender, so a
    non-Ended state sent for an evicted or tombstoned session is compensated too.
    """
    return outcome.status != STATUS_NON_RETRYABLE


def _missing_session(data: dict, tx: Transmission, outcome: Outcome, now: float) -> StagedEffects:
    if _may_have_landed(outcome) and tx.state != "Ended":
        log_debug(f"Send may have landed for evicted session {tx.session_id} ({outcome.status}); staging compensation")
        return StagedEffects(COMPENSATE, compensations=(stage_compensation(data, tx, now),))
    return StagedEffects(MISSING)


def _tombstoned(data: dict, session: dict, tx: Transmission, outcome: Outcome, now: float) -> StagedEffects:
    """Pane closed while a non-Ended send was in flight: no state advancement (Plan §4.3 Step C)."""
    if _owns_lease(session, tx):
        _clear_lease(session)
    pending = int(session.get("delivered_seq", 0) or 0) < int(session.get("seq", 0) or 0)
    if not _may_have_landed(outcome):
        return StagedEffects(TOMBSTONED, touch_pending=pending, spawn_reconciler=pending)
    if session.get("desired_state") != "Ended":
        log_debug(f"Aborting compensation for {tx.session_id}: live session re-admitted")
        return StagedEffects(COMPENSATION_ABORTED, touch_pending=pending, spawn_reconciler=pending)
    return StagedEffects(COMPENSATE, compensations=(stage_compensation(data, tx, now),),
                         touch_pending=pending, spawn_reconciler=pending)


def force_resync_superseding_senders(session: dict) -> None:
    """Re-send the session from scratch AND invalidate every in-flight send's snapshot.

    Bumping ``resync_generation`` makes any sender that is mid-flight for this session
    (whatever lease it holds) take the RESYNC branch in its Step C instead of recording
    a delivery that Bartender may have overridden (lease supersession, post-compensation
    re-sync detection).
    """
    session["resync_generation"] = int(session.get("resync_generation", 0) or 0) + 1
    _force_resync(session)


def _superseded(session: dict, tx: Transmission) -> StagedEffects:
    log_debug(f"Lease token superseded for {tx.session_id}; forcing re-sync")
    force_resync_superseding_senders(session)
    return StagedEffects(SUPERSEDED, touch_pending=True, spawn_reconciler=True)


def _late_send_after_takeover(session: dict, tx: Transmission, outcome: Outcome) -> bool:
    """Plan §1 row 6 + §4.3 Step C: our lease was taken over and the new holder delivered a newer seq.

    Our older seq may still have reached Bartender AFTER that delivery (only a rejection
    proves it did not), and Bartender renders the last state it received, so this is a
    lease supersession (forced re-sync), not a stale result.
    """
    if tx.lease_token is None or session.get("lease_token") == tx.lease_token:
        return False
    return _may_have_landed(outcome) and tx.seq < int(session.get("delivered_seq", 0) or 0)


def _is_stale(session: dict, tx: Transmission, outcome: Outcome) -> bool:
    """Below delivered_seq; a failure for an already confirmed seq; or a duplicate of the applied success."""
    delivered = int(session.get("delivered_seq", 0) or 0)
    if tx.seq < delivered:
        return True
    if tx.seq != delivered:
        return False
    return not outcome.success or session.get("delivered_state") == tx.state


def _evict(data: dict, session: dict, tx: Transmission, now_ns: int) -> None:
    """Confirmed Ended: evict, drop the marker, re-record the close origin persisted in Step A."""
    data["sessions"].pop(tx.session_id, None)
    remove_pane_marker(tx.pane_id)
    origin_ns = tx.arrival_ns or now_ns
    if not tx.pane_id:
        return
    if session.get("close_kind") == "container":  # re-affirmed from the origins persisted in Step A
        record_tombstone(data, tx.pane_id, int(session.get("closed_at_ns") or origin_ns),
                         _num(session.get("closed_source_ts")), _num(session.get("last_source_timestamp")))
    elif session.get("close_kind") == "agent_exit":
        data.setdefault("agent_exits", {})[tx.pane_id] = {
            "exit_at_ns": session.get("exit_at_ns") or origin_ns,
            "exit_source_ts": float(session.get("exit_source_ts") or 0.0),
        }


def resync_live_sessions(data: dict, exclude: Optional[str] = None) -> Tuple[str, ...]:
    """Plan §5.1 item 4 Full Top Shelf Re-Sync: re-assert every live, non-salvaged session but ``exclude``.

    Salvaged sessions stay quiescent (never transmitted as Idle); Ended ones are only re-armed.
    """
    resynced = []
    for sid, record in data.get("sessions", {}).items():
        if sid == exclude or not isinstance(record, dict):
            continue
        if record.get("desired_state") != "Ended" and not record.get("salvaged"):
            force_resync_superseding_senders(record)   # in-flight senders re-sync too (resync_generation)
            record.update({"delivery_attempts": 0, "next_retry_at": None, "delivery_error": None})
            resynced.append(sid)
    return tuple(resynced)


def _reconnection_resync(data: dict, tx: Transmission, down: Optional[bool]) -> Tuple[str, ...]:
    """A confirmed POST while DELIVERY_DOWN is set IS the bridge reconnection (Plan §1 L113, §5.1 item 4).

    Whoever confirms first - event-path Step C, a drained ``results/`` envelope, the reconciler sweep or its
    ``/health`` probe - re-syncs in the same critical section that clears the flag (after the save:
    ``commit_delivery_down``), so the re-sync can never be lost to a race over who saw DELIVERY_DOWN or to a
    failed save (the session just confirmed is not re-sent). ``down``: the flag as already decided earlier in
    this critical section (a drain batch), None to read the marker.
    """
    if not (is_delivery_down() if down is None else down):
        return ()
    resynced = resync_live_sessions(data, exclude=tx.session_id)
    log_debug(f"Bridge reconnected after DELIVERY_DOWN ({tx.session_id} confirmed); re-syncing {len(resynced)}")
    return resynced


def rearm_exhausted(data: dict) -> int:
    """Plan §5.1 item 3: a healthy bridge (any confirmed POST, or /health) re-arms every exhausted session."""
    rearmed = 0
    for other in data.get("sessions", {}).values():
        if isinstance(other, dict) and other.get("delivery_status") == "retryable_exhausted":
            other.update({"delivery_attempts": 0, "delivery_status": "in_flight", "next_retry_at": None})
            rearmed += 1
    return rearmed


def retry_delay(attempts: int) -> float:
    """Seconds between attempt ``attempts`` (>= 1) and the next one on the 0/1/2/4/8s schedule."""
    return RETRY_DELAYS[min(max(attempts, 0), MAX_DELIVERY_ATTEMPTS - 1)]


def _pane_closed(session: dict, tx: Transmission) -> bool:
    """R24: only a confirmed pane/tab/workspace close (``close_kind == "container"``) left no pane behind.

    A TTL, Herdr dead/restart, agent-exit or --cleanup Ended leaves the pane - and possibly a vendor CLI with a
    live fallback - in place, so its dismissal stays subject to the pane-scoped cancellation triggers (#60).
    """
    return tx.state == "Ended" and session.get("close_kind") == "container"


def _apply_success(data: dict, session: dict, tx: Transmission, now: float, now_ns: int,
                   down: Optional[bool]) -> StagedEffects:
    if int(session.get("resync_generation", 0) or 0) > tx.resync_generation:
        log_debug(f"Resync generation advanced during transmission of {tx.session_id}; forcing re-sync")
        _force_resync(session)
        return StagedEffects(RESYNC, touch_pending=True, spawn_reconciler=True)
    session.update({"delivered_state": tx.state, "delivered_seq": tx.seq, "delivery_status": "delivered",
                    "delivery_attempts": 0, "delivery_error": None, "next_retry_at": None})
    data["consecutive_failures"] = 0
    data["last_successful_delivery"] = now
    touch_pane_marker(tx.pane_id)
    resynced = _reconnection_resync(data, tx, down)
    clear_pane_failed(tx.pane_id)
    rearm_exhausted(data)
    evict = tx.state == "Ended" and int(session.get("seq", 0) or 0) == tx.seq
    cleanups = (stage_vendor_cleanup(data, tx.pane_id, _pane_closed(session, tx), now),) if tx.pane_id else ()
    if evict:
        _evict(data, session, tx, now_ns)
        effects = StagedEffects(EVICTED, orphans_to_remove=(tx.session_id,), vendor_cleanups=cleanups, evicted=True,
                                delivery_down=False)
    else:
        effects = StagedEffects(DELIVERED, vendor_cleanups=cleanups, delivery_down=False)
    return replace(effects, touch_pending=True, spawn_reconciler=True) if resynced else effects


def _orphan_if_ended(session: dict, tx: Transmission, desired: bool, now: float) -> Tuple[Tuple[str, dict], ...]:
    """Mark an unconfirmable Ended ``orphaned_ended`` (stamping ``orphaned_at`` once: the R12 12h horizon origin)."""
    state = session.get("desired_state") if desired else tx.state
    if state != "Ended":
        return ()
    session["orphaned_ended"] = True
    if not isinstance(session.get("orphaned_at"), (int, float)) or isinstance(session.get("orphaned_at"), bool):
        session["orphaned_at"] = now
    return ((tx.session_id, mirror_copy(session)),)


def _apply_non_retryable(data: dict, session: dict, tx: Transmission, outcome: Outcome,
                         now: float, now_ns: int) -> StagedEffects:
    session.update({"delivery_status": "non_retryable_failed", "rejected_seq": tx.seq,
                    "delivery_error": outcome.error or DEFAULT_NON_RETRYABLE_ERROR, "next_retry_at": None})
    touch_pane_failed(tx.pane_id)
    return StagedEffects(REJECTED, orphans_to_export=_orphan_if_ended(session, tx, desired=False, now=now))


def _apply_retryable(data: dict, session: dict, tx: Transmission, outcome: Outcome,
                     now: float, now_ns: int) -> StagedEffects:
    session["delivery_attempts"] = int(session.get("delivery_attempts", 0) or 0) + 1
    session["delivery_error"] = outcome.error or DEFAULT_RETRYABLE_ERROR
    touch_pane_failed(tx.pane_id)
    data["consecutive_failures"] = int(data.get("consecutive_failures", 0) or 0) + 1
    down = True if data["consecutive_failures"] >= DELIVERY_DOWN_THRESHOLD else None
    if session["delivery_attempts"] < MAX_DELIVERY_ATTEMPTS:
        # The lease is released by _finalize; the reconciler re-claims it once this time has passed.
        session["next_retry_at"] = now + retry_delay(session["delivery_attempts"])
        return StagedEffects(RETRY, spawn_reconciler=True, delivery_down=down)
    session.update({"delivery_status": "retryable_exhausted", "next_retry_at": None})
    return StagedEffects(EXHAUSTED, orphans_to_export=_orphan_if_ended(session, tx, desired=True, now=now),
                         spawn_reconciler=True, delivery_down=down)


_FAILURE_BRANCHES: Dict[str, Callable[..., StagedEffects]] = {
    STATUS_NON_RETRYABLE: _apply_non_retryable,
    STATUS_RETRYABLE: _apply_retryable,
}


def _apply_outcome(data: dict, session: dict, tx: Transmission, outcome: Outcome, now: float, now_ns: int,
                   down: Optional[bool]) -> StagedEffects:
    """The §3.3 response matrix branch for ``outcome``."""
    if outcome.success:
        return _apply_success(data, session, tx, now, now_ns, down)
    return _FAILURE_BRANCHES[outcome.status](data, session, tx, outcome, now, now_ns)


def _finalize(session: dict, tx: Transmission, effects: StagedEffects, retain_lease: bool,
              outcome: Outcome, now: float) -> StagedEffects:
    """Plan §4.3 'Check if newer state arrived': keep the lease for a follow-up send, or clear it."""
    undelivered = int(session.get("delivered_seq", 0) or 0) < int(session.get("seq", 0) or 0)
    status = session.get("delivery_status")
    sendable = undelivered and status not in ("non_retryable_failed", "retryable_exhausted")
    owns = _owns_lease(session, tx)
    if retain_lease and owns and sendable and outcome.success:
        session["lease_deadline"] = now + LEASE_SECONDS
        return replace(effects, followup=True)
    if owns:
        _clear_lease(session)
    handoff = undelivered and status != "non_retryable_failed"
    return replace(effects, touch_pending=effects.touch_pending or handoff,
                   spawn_reconciler=effects.spawn_reconciler or handoff)


def apply_delivery_result(data: dict, tx: Transmission, outcome: Outcome, *, retain_lease: bool = False,
                          now: Optional[float] = None, now_ns: Optional[int] = None,
                          delivery_down: Optional[bool] = None) -> StagedEffects:
    """Apply one delivery outcome to ``data`` under the cache lock (Plan §3.3 + §4.3 Step C).

    Order: missing session -> compensation; tombstoned non-Ended -> compensation /
    abort (no advancement; only a non_retryable rejection skips compensating);
    a taken-over lease whose older seq may have landed after the new holder's
    delivery -> forced re-sync (Plan §1 row 6);
    stale (transmitting_seq below delivered_seq, a failure at delivered_seq, or a
    duplicate of the success already applied) -> ignored;
    lease token mismatch -> forced re-sync; then the success / non_retryable /
    retryable branch. Unless ``retain_lease`` (caller sends the newer seq right
    away), the caller's own lease is cleared and any remaining undelivered seq is
    flagged for the reconciler (``touch_pending`` / ``spawn_reconciler``).

    The DELIVERY_DOWN transition is returned (``effects.delivery_down``), never applied: the
    caller runs ``commit_delivery_down(effects)`` once ``data`` is saved. A caller applying
    several results in one critical section passes the merged transition so far as
    ``delivery_down`` (None: read the marker).
    """
    if outcome.status not in RESULT_STATUSES:
        raise ValueError(f"unknown delivery status {outcome.status!r}")
    now = clock.time() if now is None else now
    now_ns = clock.time_ns() if now_ns is None else now_ns
    session = data.setdefault("sessions", {}).get(tx.session_id)
    if session is None:
        return _missing_session(data, tx, outcome, now)
    if tx.state != "Ended" and tx.pane_id and tx.pane_id in data.get("tombstones", {}):
        return _tombstoned(data, session, tx, outcome, now)
    if _late_send_after_takeover(session, tx, outcome):
        return _superseded(session, tx)
    if _is_stale(session, tx, outcome):
        return StagedEffects(STALE)
    if tx.lease_token is not None and session.get("lease_token") != tx.lease_token:
        return _superseded(session, tx)
    effects = _apply_outcome(data, session, tx, outcome, now, now_ns, delivery_down)
    if effects.evicted:
        return effects
    return _finalize(session, tx, effects, retain_lease, outcome, now)


def commit_delivery_down(effects: StagedEffects) -> None:
    """Apply the staged DELIVERY_DOWN transition; call it right after ``cache_mgr.save`` succeeded, under the lock.

    A reconnection clears the flag only once its Full Re-Sync is saved: when the save fails the flag stays, so the
    confirmation applied again (results drain, retried replay) or the reconciler's ``/health`` recovery still
    re-syncs every other live session.
    """
    if effects.delivery_down is True:
        touch_delivery_down()
    elif effects.delivery_down is False:
        clear_delivery_down()


def journal_owed_exports(effects: StagedEffects, data: dict) -> bool:
    """Under the cache lock, BEFORE the save that marks them ``orphaned_ended``: journal the owed exports durably.

    ``orphaned_ended`` makes a record evictable at the 256 cap without another export (``cache.safe_to_evict``),
    so a crash between that save and the export after the lock would drop the owed Ended. The fsynced R10 journal
    entry closes that window (the Step A pattern); a save that then fails leaves only a harmless duplicate mirror.
    False (nothing journaled): no exports, or the journal failed; ``run_orphan_effects`` then exports after the save
    and the records stay flagged ``ORPHAN_MIRROR_OWED`` in ``data`` (not cap-evictable) - see
    ``journal_exports_before_save``.
    """
    return journal_exports_before_save(data, effects.orphans_to_export)


def journal_exports_before_save(data: dict, exports: Tuple[Tuple[str, dict], ...]) -> bool:
    """Journal ``exports`` (fsynced, no orphan lock) before the save that marks them; True when journaled.

    On failure each exported record still in ``data`` is flagged ``ORPHAN_MIRROR_OWED`` so the save does not make
    it cap-evictable while the cache holds the only copy of its Ended; on success the flag is dropped (the export
    is durable now).
    """
    if not exports:
        return False
    try:
        journal_orphan_exports(exports)
        journaled = True
    except (OSError, TypeError, ValueError) as exc:
        log_warning(f"Could not journal {len(exports)} orphan export(s) before the save ({exc}); exporting after it "
                    "instead and keeping the record(s) out of the cap prune until an export is confirmed")
        journaled = False
    sessions = data.get("sessions", {})
    for sid, _ in exports:
        record = sessions.get(sid)
        if not isinstance(record, dict):
            continue
        if journaled:
            record.pop(ORPHAN_MIRROR_OWED, None)
        else:
            record[ORPHAN_MIRROR_OWED] = True
    return journaled


def run_orphan_effects(effects: StagedEffects, blocking: bool = False, journaled: bool = False) -> None:
    """Execute staged orphan exports/removals OUTSIDE the cache lock (Plan §1 L108).

    On orphan-lock contention the orphans module journals the operation for the
    reconciler (R10), so nothing is lost. ``journaled``: the exports already wait in the
    journal (``journal_owed_exports``), so they are folded first and only the removals follow.
    """
    if journaled:
        flush_pending_orphan_ops(blocking=blocking)
        run_orphan_io((), effects.orphans_to_remove, blocking=blocking)
        return
    run_orphan_io(effects.orphans_to_export, effects.orphans_to_remove, blocking=blocking)


def empty_effects() -> StagedEffects:
    return StagedEffects("none")


def merge_all(effects: List[StagedEffects]) -> StagedEffects:
    merged = empty_effects()
    for item in effects:
        merged = merged.merge(item)
    return merged


__all__ = [
    "Outcome", "StagedEffects", "Transmission", "apply_delivery_result", "clear_pending_compensation",
    "clear_pending_vendor_cleanup", "commit_delivery_down", "empty_effects", "force_resync_superseding_senders",
    "foreign_lease_open", "journal_exports_before_save", "journal_owed_exports", "merge_all", "rearm_exhausted", "record_tombstone",
    "resync_live_sessions", "retry_delay", "run_orphan_effects",
    "stage_compensation", "stage_vendor_cleanup",
]
