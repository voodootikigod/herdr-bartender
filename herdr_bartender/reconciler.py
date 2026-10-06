"""One reconcile pass through the Universal Sender (Plan §5.1 items 1, 2, 3, 5a, 5b, 7-9, 12; R10-R14, R19).

``reconcile_active_sessions()`` is the delivery half of a reconciler pass; ``background``
wraps it with the drains, health recovery, orphan replay, heartbeat and loop timing.

1. Persisted side effects: owed compensating Endeds (``sender.compensate``: re-verified
   under the lock, cleared only after a landed POST, exported once exhausted) and
   ``pending_vendor_cleanups`` (resolved into the dismissal queue, never dropped).
2. Lifecycle under ONE lock hold with a pre-lock process snapshot (no subprocess and no
   orphan I/O under the lock): Bartender/Herdr restarts (R14), Herdr dead >5m, TTLs,
   salvage horizon, payload consistency (``lifecycle``).
3. R12 horizon: unconfirmed Endeds 12h past their TTL expiry / orphaning are exported to
   the orphan file OUTSIDE the lock (R10 blocking mode, bounded), then evicted if unchanged.
4. Delivery sweep: every due session is claimed ONE AT A TIME under the lock with the
   6-row lease truth table (+0.5s grace), sent with ``BACKGROUND_POLICY`` (0.2s socket,
   no event-path budget) and settled by the shared Step C. A retryable failure persists
   ``next_retry_at`` (0/1/2/4/8s) and releases the lease; the loop sleeps until it is due
   and re-claims under the lock (token verified in Step C).
5. Vendor dismissals: the 12h ``.vendor_active`` horizon, then the dismissal queue sweep
   (cancellation triggers, 2.0s cadence, 5 attempts, 10s window, agent "Herdr").
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Mapping, Optional, Tuple

from . import clock
from .cache import ORPHAN_MIRROR_OWED, BoundedSessionCache, IntegrationDisabled
from .delivery_state import foreign_lease_open
from .dismissals import dismiss_stale_vendor_files, sweep_dismissals
from .handoff import touch_reconciler_pending
from .lifecycle import (
    LifecycleReport,
    apply_lifecycle,
    ensure_payload,
    evict_exported,
    horizon_exports,
    is_live,
    mirror_owed_records,
    orphan_record,
    same_record,
    sendable,
)
from .log import log_debug, log_warning
from .markers import remove_pane_marker
from .orphans import export_orphan_record, exported_session_records, journaled_export_records, orphan_pane_ids
from .process import own_start_time
from .sender import (
    BACKGROUND_POLICY,
    Claim,
    SendPolicy,
    claim_lease,
    compensate,
    deliver_claim,
    dismiss_vendors,
    lease_active_elsewhere,
    warm_step_a_probes,
)
from .sender.compensation import valid_entry as valid_compensation
from .snapshot import ProcessSnapshot, take_snapshot
from .vendor import pending_vendor_panes, resolve_vendor_cleanups, warm_fallback_probe

Between = Optional[Callable[[], None]]   # called between sends (the background loop's marker heartbeat)


@dataclass(frozen=True)
class ReconcileReport:
    lifecycle: LifecycleReport = LifecycleReport()
    evicted: Tuple[str, ...] = ()
    attempted: Tuple[str, ...] = ()
    hand_off: bool = False


# -- persisted side effects (§5.1 item 5a) -----------------------------------------------
def _owed_compensations(cache_mgr: BoundedSessionCache) -> list:
    """Snapshot of the well-formed owed compensations; malformed entries are purged (they could never be sent)."""
    with cache_mgr as data:
        entries = data.get("pending_compensations", [])
        valid = [c for c in entries if valid_compensation(c)]
        if len(valid) != len(entries):
            log_debug(f"Dropping {len(entries) - len(valid)} malformed pending_compensations entries")
            data["pending_compensations"] = valid
            cache_mgr.save(data)
    return valid


def _tick(between: Between) -> None:
    if between is not None:
        between()


def drain_compensations(cache_mgr: BoundedSessionCache, bridge_url: Optional[str] = None,
                        policy: SendPolicy = BACKGROUND_POLICY, between: Between = None) -> bool:
    """Drain persisted compensations through the Universal Sender; True when ``reconciler.pending`` was flagged.

    Each entry gets the under-lock re-verification (target generation, retry schedule), its attempt recorded, the
    POST, then clear + re-sync detection; it is cleared only by an abort, a landed POST or its export to the orphan
    file once its attempts are exhausted. A forced re-sync (a re-admission an Ended may have dismissed) flags another
    pass. An owed entry is NOT re-flagged: the loop sleeps until it is due.
    """
    need = False
    for comp in _owed_compensations(cache_mgr):
        need = compensate(cache_mgr, comp, policy, bridge_url) or need
        _tick(between)
    if need:
        touch_reconciler_pending()
    return need


def queue_pending_vendor_cleanups(cache_mgr: BoundedSessionCache) -> Tuple[str, ...]:
    """Resolve persisted ``pending_vendor_cleanups`` into the dismissal queue (stage before send, never drop).

    While the vendor fallback is protected (Herdr dead, NO_HOOKS: R35) they are cancelled instead.
    """
    warm_fallback_probe()
    with cache_mgr as data:
        resolution = resolve_vendor_cleanups(data, pending_vendor_panes(data), clock.time())
        if resolution.changed:
            cache_mgr.save(data)
    resolution.commit()   # R88: vendor-file I/O only after the lock is released (and only after the save)
    return resolution.dismissals


# -- lifecycle + R12 horizon -------------------------------------------------------------------
Exports = Tuple[Tuple[str, dict], ...]


def _apply_lifecycle(cache_mgr: BoundedSessionCache, snap: ProcessSnapshot) -> Tuple[LifecycleReport, Exports, Exports]:
    """(report, R12 horizon exports, R52 records whose orphan export is still owed)."""
    orphan_panes = orphan_pane_ids()  # read outside the cache lock (gap save-reads-orphans-under-lock)
    with cache_mgr as data:
        now = clock.time()
        report = apply_lifecycle(data, snap, now)
        for sid, record in data.get("sessions", {}).items():
            if isinstance(record, dict) and not record.get("salvaged"):
                ensure_payload(sid, record)
        exports = horizon_exports(data, now)
        owed = mirror_owed_records(data)
        cache_mgr.save(data, orphan_panes=orphan_panes)
    return report, exports, owed


def _evict_locked(cache_mgr: BoundedSessionCache, exported) -> Tuple[str, ...]:
    with cache_mgr as data:
        evicted = evict_exported(data, exported)
        live_panes = {r.get("pane_id") for r in data.get("sessions", {}).values() if isinstance(r, dict) and is_live(r)}
        if evicted:
            cache_mgr.save(data)
        for _, pane in evicted:
            if pane and pane not in live_panes:
                remove_pane_marker(pane)
    return tuple(sid for sid, _ in evicted)


def evict_exported_sessions(cache_mgr: BoundedSessionCache, exports, reason: str) -> Tuple[str, ...]:
    """Export each record to the orphan file (blocking, bounded: R10), then evict the unchanged exported ones.

    Only a record really written to the orphan file is evicted. A session whose export already waits in the
    journal (an earlier pass could not write the file) is not exported again until the journal is folded: a
    persistent orphan-file error never journals the same export every pass.
    """
    waiting = journaled_export_records()
    exported = [(sid, record) for sid, record in exports
                if not _holds_export(waiting, sid, record)
                and export_orphan_record(sid, orphan_record(record), blocking=True)]
    if len(exported) != len(exports):
        log_warning(f"{len(exports) - len(exported)} {reason} export(s) not written; kept for the next pass")
    if not exported:
        return ()
    evicted = _evict_locked(cache_mgr, exported)
    for sid in evicted:
        log_warning(f"Evicted {sid} after exporting it to the orphan file ({reason})")
    return evicted


def _holds_export(exported: Mapping[str, List[dict]], sid: str, record: Mapping) -> bool:
    """R77: a durable export of THIS record (same seq and desired state), not merely of the same session id."""
    wanted = orphan_record(record)
    return any(same_record(candidate, wanted) for candidate in exported.get(sid, ()))


def remirror_owed_sessions(cache_mgr: BoundedSessionCache, owed: Exports) -> Tuple[str, ...]:
    """R52: make each owed orphan export durable (blocking, bounded: R10), then clear ``ORPHAN_MIRROR_OWED`` on the
    records that did not move on meanwhile, so the cap may prune them again.

    An export already waiting in the journal is durable as it is (no rewrite every pass while the orphan file stays
    unwritable); one written to the file or journaled now counts too. Nothing durable: the flag stays.
    """
    waiting = journaled_export_records()
    for sid, record in owed:
        if not _holds_export(waiting, sid, record):
            export_orphan_record(sid, orphan_record(record), blocking=True)
    exported = exported_session_records()
    durable = [(sid, record) for sid, record in owed if _holds_export(exported, sid, record)]
    if not durable:
        log_warning(f"{len(owed)} orphan export(s) still not durable; their records stay out of the cap prune")
        return ()
    cleared = []
    with cache_mgr as data:
        sessions = data.get("sessions", {})
        for sid, record in durable:
            current = sessions.get(sid)
            if same_record(current, record) and current.pop(ORPHAN_MIRROR_OWED, None) is not None:
                cleared.append(sid)
        if cleared:
            cache_mgr.save(data)
    return tuple(cleared)


# -- delivery sweep (§5.1 item 2 / item 3) ---------------------------------------------------------
def due_sessions(data: dict, now: float) -> Tuple[str, ...]:
    return tuple(sid for sid, rec in data.get("sessions", {}).items() if isinstance(rec, dict) and sendable(rec, now))


def claim_due(cache_mgr: BoundedSessionCache, session_id: str) -> Optional[Claim]:
    """Claim one session under the lock: re-checked due, lease truth table rows 1-6 (+0.5s grace)."""
    with cache_mgr as data:
        now = clock.time()
        record = data.get("sessions", {}).get(session_id)
        if not isinstance(record, dict) or not sendable(record, now):
            return None
        if lease_active_elsewhere(record, now):
            log_debug(f"{session_id} is leased by a live sender (rows 4/5); deferred to its deadline + grace")
            return None
        ensure_payload(session_id, record)
        claim = claim_lease(session_id, record.get("pane_id"), record, now, None)
        cache_mgr.save(data)
    return claim


def deliver_due_sessions(cache_mgr: BoundedSessionCache, bridge_url: Optional[str] = None,
                         policy: SendPolicy = BACKGROUND_POLICY, between: Between = None
                         ) -> Tuple[Tuple[str, ...], bool]:
    """Claim, send and settle every due session, one lease at a time; (attempted sids, hand-off wanted)."""
    with cache_mgr as data:
        candidates = due_sessions(data, clock.time())
    if not candidates:
        return (), False
    warm_step_a_probes(cache_mgr.state_dir, clock.time())  # lease holders' start times, before any lock
    attempted, hand_off = [], False
    for session_id in candidates:
        claim = claim_due(cache_mgr, session_id)
        if claim is None:
            continue
        attempted.append(session_id)
        hand_off = deliver_claim(cache_mgr, claim, policy=policy, bridge_url=bridge_url).hand_off or hand_off
        _tick(between)
    return tuple(attempted), hand_off


def _flag_if_due_now(cache_mgr: BoundedSessionCache) -> None:
    """A Step C re-sync left a seq due right now: run the next pass at once (otherwise the loop sleeps until due).

    A session another live sender holds is not "due now" (it is re-claimed at its deadline + grace), so a
    deferral never turns the loop into a busy re-run.
    """
    with cache_mgr as data:
        now = clock.time()
        sessions = data.get("sessions", {})
        if any(not foreign_lease_open(sessions[sid], now) for sid in due_sessions(data, now)):
            touch_reconciler_pending()


# -- the pass -------------------------------------------------------------------------------------
def _dismissals(cache_mgr: BoundedSessionCache, snap: ProcessSnapshot, bridge_url: Optional[str],
                policy: SendPolicy) -> None:
    dismiss_stale_vendor_files(cache_mgr, snap.herdr_alive, clock.time())
    sweep_dismissals(cache_mgr, snap.herdr_alive,
                     lambda uuids: dismiss_vendors(cache_mgr, uuids, policy, bridge_url))


def _reconcile(state_dir: Path, bridge_url: Optional[str], snap: Optional[ProcessSnapshot],
               policy: SendPolicy, between: Between) -> ReconcileReport:
    own_start_time()  # the lease-token start time, warmed outside any critical section
    cache_mgr = policy.cache(state_dir)
    drain_compensations(cache_mgr, bridge_url, policy, between)
    queue_pending_vendor_cleanups(cache_mgr)
    snap = snap or take_snapshot(state_dir)
    report, exports, owed = _apply_lifecycle(cache_mgr, snap)
    evicted = evict_exported_sessions(cache_mgr, exports, "12h past TTL, R12") if exports else ()
    owed = tuple((sid, record) for sid, record in owed if sid not in evicted)
    if owed:
        remirror_owed_sessions(cache_mgr, owed)
    attempted, hand_off = deliver_due_sessions(cache_mgr, bridge_url, policy, between)
    if hand_off:
        _flag_if_due_now(cache_mgr)
    _dismissals(cache_mgr, snap, bridge_url, policy)
    return ReconcileReport(report, evicted, attempted, hand_off)


def reconcile_active_sessions(state_dir: Path, bridge_url: Optional[str] = None,
                              snapshot: Optional[ProcessSnapshot] = None,
                              policy: SendPolicy = BACKGROUND_POLICY, between: Between = None) -> ReconcileReport:
    """One reconcile pass (raises CacheError for the loop's backoff; DISABLED ends it quietly).

    ``between`` runs after every compensation and session send (the loop's marker heartbeat in a long pass).
    """
    try:
        return _reconcile(Path(state_dir), bridge_url, snapshot, policy, between)
    except IntegrationDisabled:
        log_debug("DISABLED under the cache lock; reconcile pass stopped")
        return ReconcileReport()

