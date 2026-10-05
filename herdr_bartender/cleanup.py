"""--cleanup: end every tracked session on shutdown/rollback (Plan §5.2, §9.1 step 3, §1 exit-code contract).

* Exempt from the 1.5s watchdog (runs in ``DEADLINE_UNBOUNDED``) and bypasses DISABLED
  (the rollback script sets DISABLED first).
* Every session - salvaged ones included - is staged to Ended (next seq) under one lock
  hold, then delivered through the Universal Sender (gap cleanup-not-universal-sender):
  per-session lease claim with the 6-row truth table, a 0.15s socket timeout, the minimal
  Ended retry, the shared Step C and post-lock dispatch. The whole run is bounded by
  ``max(10.0, n * 0.15)`` seconds (``sender.cleanup_policy``); a session leased by a live
  sender is retried until its lease lapses or the budget ends.
* The orphan journal is flushed before and after.

Exit codes:
  0 - every session confirmed Ended by the bridge (HTTP 200);
  2 - the bridge was unreachable or rejected some: the unconfirmed sessions keep their
      failure state in the cache (``orphaned_ended``, ``orphaned_at``, ``delivery_status``,
      ``delivery_error``; gap cleanup-failure-state) and are exported to
      ``$HOME/.herdr-bartender-orphans.json`` (0600) for ``--replay-orphans``. Only the
      Endeds this run staged (still Ended, seq not below the staged one) are marked and
      exported: a session re-admitted live meanwhile - or admitted afresh - is left as it is
      (its delivery state and marker heartbeat intact), but still makes the exit code 2;
  1 - fatal error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Tuple

from . import clock, runtime
from .cache import UNBOUNDED_LOCK_TIMEOUT, BoundedSessionCache, CacheError
from .delivery_state import journal_exports_before_save
from .lifecycle import expire_session, orphan_record, sendable
from .log import log_debug, log_warning
from .markers import touch_heartbeat
from .orphans import export_orphan_record, flush_pending_orphan_ops
from .paths import get_state_dir
from .process import own_start_time
from .sender import (
    Claim,
    SendPolicy,
    claim_lease,
    cleanup_policy,
    lease_active_elsewhere,
    run_post_lock,
    settle,
    transmit,
    warm_step_a_probes,
)
from .sender.lease import peek_cache

EXIT_OK, EXIT_FATAL, EXIT_UNCONFIRMED = 0, 1, 2
DEFERRED_RETRY_PAUSE_SECONDS = 0.05
CLEANUP_UNCONFIRMED_ERROR = "cleanup_unconfirmed"


@dataclass
class _Progress:
    """Per-run bookkeeping (local to ``run_cleanup``)."""

    targeted: Mapping[str, int] = field(default_factory=dict)   # session id -> the seq of the Ended staged
    # session id -> seq of an Ended the bridge confirmed whose Step C went to results/ (the reconciler applies it)
    landed_unrecorded: Dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class _Leftovers:
    exports: Tuple[Tuple[str, dict], ...] = ()   # this run's Endeds left unconfirmed (marked, to export)
    others: Tuple[str, ...] = ()                 # re-admitted live or admitted afresh meanwhile (left alone)

    def __bool__(self) -> bool:
        return bool(self.exports or self.others)


def _stage_all_ended(cache_mgr: BoundedSessionCache) -> Dict[str, int]:
    """Every cached session to Ended (next seq), salvaged ones included, under one lock hold."""
    with cache_mgr as data:
        now = clock.time()
        sessions = data.get("sessions", {})
        for sid, record in sessions.items():
            if isinstance(record, dict):
                expire_session(sid, record, now, "cleanup", horizon=False)
        if sessions:
            cache_mgr.save(data)
        return {sid: int(record.get("seq", 0) or 0) for sid, record in sessions.items() if isinstance(record, dict)}


def _claim(cache_mgr: BoundedSessionCache, sid: str) -> Tuple[Optional[Claim], bool]:
    """(claim, deferred): a Claim; or (None, True) while a live sender holds the lease (rows 4/5);
    or (None, False) when the session is gone or has nothing left to send."""
    with cache_mgr as data:
        now = clock.time()
        record = data.get("sessions", {}).get(sid)
        if not isinstance(record, dict) or not sendable(record, now):
            return None, False
        if lease_active_elsewhere(record, now):
            return None, True
        claim = claim_lease(sid, record.get("pane_id"), record, now, None)
        cache_mgr.save(data)
    return claim, False


def _deliver(cache_mgr: BoundedSessionCache, claim: Claim, policy: SendPolicy, bridge_url: Optional[str],
             progress: _Progress) -> None:
    sent = transmit(claim, policy, bridge_url)
    post = settle(cache_mgr, claim, sent, policy)
    run_post_lock(cache_mgr, post, policy, bridge_url)
    if sent.transmitted and sent.result.success and post.live_remaining is None:
        progress.landed_unrecorded[claim.session_id] = claim.seq   # Step C deferred to results/


def _round(cache_mgr: BoundedSessionCache, outstanding: List[str], policy: SendPolicy,
           bridge_url: Optional[str], progress: _Progress) -> List[str]:
    """One attempt per outstanding session; returns the sessions deferred by a live lease."""
    warm_step_a_probes(cache_mgr.state_dir, clock.time())
    deferred = []
    for sid in outstanding:
        if not policy.allows_network():
            break
        claim, leased_elsewhere = _claim(cache_mgr, sid)
        if leased_elsewhere:
            deferred.append(sid)
        elif claim is not None:
            _deliver(cache_mgr, claim, policy, bridge_url, progress)
    return deferred


def _deliver_all(cache_mgr: BoundedSessionCache, policy: SendPolicy, bridge_url: Optional[str],
                 progress: _Progress) -> None:
    outstanding = list(progress.targeted)
    while outstanding and policy.allows_network():
        outstanding = _round(cache_mgr, outstanding, policy, bridge_url, progress)
        if outstanding:
            clock.sleep(DEFERRED_RETRY_PAUSE_SECONDS)
    if outstanding:
        log_warning(f"--cleanup budget spent with {len(outstanding)} session(s) still leased by another sender")


def _mark_unconfirmed(record: dict, now: float) -> None:
    """Gap cleanup-failure-state: record why the session is still cached before it is exported."""
    record["orphaned_ended"] = True
    if not isinstance(record.get("orphaned_at"), (int, float)):
        record["orphaned_at"] = now
    if record.get("delivery_status") not in ("non_retryable_failed", "retryable_exhausted"):
        record["delivery_status"] = "retryable_exhausted"   # cleanup does not retry it; a healthy bridge re-arms it
    record["delivery_error"] = record.get("delivery_error") or CLEANUP_UNCONFIRMED_ERROR


def _owed_ended(sid: str, record: dict, progress: _Progress) -> bool:
    """One of this run's Endeds, still unconfirmed: still Ended, at or past the seq this run staged."""
    staged = progress.targeted.get(sid)
    return staged is not None and record.get("desired_state") == "Ended" \
        and int(record.get("seq", 0) or 0) >= staged


def _landed(sid: str, record: dict, progress: _Progress) -> bool:
    """Confirmed by the bridge (Step C in results/) and not moved on since: still that Ended, no newer seq.

    A session re-admitted live after its Ended landed (or ended again at a newer, unconfirmed seq) is not
    confirmed: R31 still makes the exit code 2 for it.
    """
    landed = progress.landed_unrecorded.get(sid)
    return landed is not None and record.get("desired_state") == "Ended" \
        and int(record.get("seq", 0) or 0) <= landed


def _sort_leftovers(sessions: Mapping, progress: _Progress, mark_at: Optional[float]) -> _Leftovers:
    """Split the cached sessions into this run's unconfirmed Endeds (marked when ``mark_at``) and the others."""
    exports, others = [], []
    for sid, record in sessions.items():
        if not isinstance(record, dict) or _landed(sid, record, progress):
            continue
        if not _owed_ended(sid, record, progress):
            others.append(sid)
            continue
        if mark_at is not None:
            _mark_unconfirmed(record, mark_at)
        exports.append((sid, orphan_record(record)))
    return _Leftovers(tuple(exports), tuple(others))


def _leftovers_locked(cache_mgr: BoundedSessionCache, progress: _Progress) -> _Leftovers:
    with cache_mgr as data:
        leftovers = _sort_leftovers(data.get("sessions", {}), progress, clock.time())
        if leftovers.exports:
            # Durable before the save marks them orphaned_ended (cap-evictable); a failed journal flags them instead.
            journal_exports_before_save(data, leftovers.exports)
            cache_mgr.save(data)
    return leftovers


def _leftovers(cache_mgr: BoundedSessionCache, progress: _Progress) -> _Leftovers:
    """Sessions not confirmed Ended; from a lock-free peek when the cache cannot be locked any more."""
    try:
        return _leftovers_locked(cache_mgr, progress)
    except CacheError as exc:
        log_warning(f"--cleanup could not record failure state ({exc}); exporting from the last saved cache")
        return _sort_leftovers(peek_cache(cache_mgr.state_dir).get("sessions") or {}, progress, None)


def _export(leftovers: Tuple[Tuple[str, dict], ...]) -> None:
    for sid, record in leftovers:
        export_orphan_record(sid, record, blocking=True)   # R10: --cleanup may block (bounded)
    if not flush_pending_orphan_ops(blocking=True):
        log_warning("Orphan journal not fully flushed by --cleanup; the next lock holder folds it in")


def _cleanup(bridge_url: Optional[str]) -> int:
    touch_heartbeat()
    own_start_time()  # the lease-token start time, outside any critical section
    flush_pending_orphan_ops(blocking=True)
    # DISABLED is never consulted: --cleanup runs after the rollback set it (Plan §5.2).
    cache_mgr = BoundedSessionCache(get_state_dir(), lock_timeout=UNBOUNDED_LOCK_TIMEOUT, check_disabled=False)
    progress = _Progress(targeted=_stage_all_ended(cache_mgr))
    policy = cleanup_policy(len(progress.targeted))
    _deliver_all(cache_mgr, policy, bridge_url, progress)
    leftovers = _leftovers(cache_mgr, progress)
    if not leftovers:
        log_debug(f"--cleanup confirmed {len(progress.targeted)} session(s) Ended")
        return EXIT_OK
    if leftovers.exports:
        _export(leftovers.exports)
        log_warning(f"--cleanup left {len(leftovers.exports)} session(s) unconfirmed; exported to the orphan file")
    if leftovers.others:
        log_warning(f"--cleanup: {len(leftovers.others)} session(s) admitted while it ran were left live")
    return EXIT_UNCONFIRMED


def run_cleanup(bridge_url: Optional[str] = None) -> int:
    """Clean up all sessions; returns the Plan §5.2 exit code (0 confirmed, 2 unconfirmed + exported, 1 fatal)."""
    with runtime.deadline_mode(runtime.DEADLINE_UNBOUNDED):
        try:
            return _cleanup(bridge_url)
        except Exception as exc:  # the exit-code contract: anything unexpected is a fatal error (exit 1)
            log_warning(f"Fatal error during cleanup: {exc!r}")
            return EXIT_FATAL
