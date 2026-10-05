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
from .orphans import ORPHAN_BLOCKING_DEADLINE_SECONDS, export_orphan_record, flush_pending_orphan_ops
from .paths import get_state_dir
from .process import own_start_time
from .results import drain_results_dir
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
from .sender.policy import cleanup_budget

EXIT_OK, EXIT_FATAL, EXIT_UNCONFIRMED = 0, 1, 2
DEFERRED_RETRY_PAUSE_SECONDS = 0.05
CLEANUP_UNCONFIRMED_ERROR = "cleanup_unconfirmed"
MAX_RESULT_DRAIN_PASSES = 32   # results drain applies a bounded batch per pass


@dataclass
class _Progress:
    """Per-run bookkeeping (local to ``run_cleanup``)."""

    targeted: Mapping[str, int] = field(default_factory=dict)   # session id -> the seq of the Ended staged


@dataclass(frozen=True)
class _Leftovers:
    exports: Tuple[Tuple[str, dict], ...] = ()   # this run's Endeds left unconfirmed (marked, to export)
    others: Tuple[str, ...] = ()                 # re-admitted live or admitted afresh meanwhile (left alone)

    def __bool__(self) -> bool:
        return bool(self.exports or self.others)


class _DeadlineCache(BoundedSessionCache):
    """R69: every cache-lock wait of a --cleanup run is clamped to what is left of its overall budget."""

    def __init__(self, state_dir, deadline: float):
        super().__init__(state_dir, lock_timeout=UNBOUNDED_LOCK_TIMEOUT, check_disabled=False)
        self.deadline = deadline

    def effective_lock_timeout(self) -> float:
        return max(0.0, min(UNBOUNDED_LOCK_TIMEOUT, self.deadline - clock.monotonic()))


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


def _round(cache_mgr: BoundedSessionCache, outstanding: List[str], policy: SendPolicy,
           bridge_url: Optional[str], progress: _Progress) -> List[str]:
    """One attempt per outstanding session; returns the sessions deferred by a live lease."""
    warm_step_a_probes(cache_mgr.state_dir, clock.time())
    deferred = []
    for sid in outstanding:
        if not policy.allows_network():
            break
        try:
            claim, leased_elsewhere = _claim(cache_mgr, sid)
            if leased_elsewhere:
                deferred.append(sid)
            elif claim is not None:
                _deliver(cache_mgr, claim, policy, bridge_url, progress)
        except CacheError as exc:   # R69: a lock wait past the budget leaves the session unconfirmed (exit 2)
            log_warning(f"--cleanup could not lock the cache for {sid} ({exc}); leaving it unconfirmed")
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


def _sort_leftovers(sessions: Mapping, progress: _Progress, mark_at: Optional[float]) -> _Leftovers:
    """Split the cached sessions into this run's unconfirmed Endeds (marked when ``mark_at``) and the others."""
    exports, others = [], []
    for sid, record in sessions.items():
        if not isinstance(record, dict):
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


def _export(leftovers: Tuple[Tuple[str, dict], ...], deadline: float) -> None:
    for sid, record in leftovers:
        # R10/R69: block on the orphan lock only while the budget lasts; past it, a contended export is journaled
        # (durable) for the next lock holder instead of waiting.
        blocking = deadline - clock.monotonic() > ORPHAN_BLOCKING_DEADLINE_SECONDS
        export_orphan_record(sid, record, blocking=blocking)
    if not flush_pending_orphan_ops(blocking=True):
        log_warning("Orphan journal not fully flushed by --cleanup; the next lock holder folds it in")


def _fold_deferred_confirmations(cache_mgr: BoundedSessionCache) -> None:
    """R58: a Step C confirmation deferred to results/ only counts once it is applied to the cache.

    Until then the session is still an unconfirmed Ended, so it is exported and --cleanup exits 2 - the rollback
    never deletes the state dir while the only durable proof of an Ended lives under it.
    """
    for _ in range(MAX_RESULT_DRAIN_PASSES):
        try:
            if not drain_results_dir(cache_mgr.state_dir, cache_mgr=cache_mgr).applied:
                return
        except CacheError as exc:
            log_warning(f"--cleanup could not apply deferred confirmations ({exc}); treating them as unconfirmed")
            return


def _unstaged_exit(state_dir, deadline: float) -> int:
    """R69: the cache could not be locked to stage the Endeds within the budget - export every cached session
    from a lock-free snapshot (unconfirmed, exit 2), or exit 0 when there is nothing to clean."""
    sessions = peek_cache(state_dir).get("sessions") or {}
    exports = tuple((sid, orphan_record(rec)) for sid, rec in sessions.items() if isinstance(rec, dict))
    if not exports:
        return EXIT_OK
    _export(exports, deadline)
    log_warning(f"--cleanup could not stage {len(exports)} session(s); exported them to the orphan file")
    return EXIT_UNCONFIRMED


def _cleanup(bridge_url: Optional[str]) -> int:
    touch_heartbeat()
    own_start_time()  # the lease-token start time, outside any critical section
    state_dir = get_state_dir()
    # R69: one budget for the whole run (sized lock-free), covering the journal flush, staging and every lock wait.
    deadline = clock.monotonic() + cleanup_budget(len(peek_cache(state_dir).get("sessions") or {}))
    flush_pending_orphan_ops(blocking=True)
    # DISABLED is never consulted: --cleanup runs after the rollback set it (Plan §5.2).
    cache_mgr = _DeadlineCache(state_dir, deadline)
    try:
        progress = _Progress(targeted=_stage_all_ended(cache_mgr))
    except CacheError as exc:
        log_warning(f"--cleanup could not stage the Endeds ({exc})")
        return _unstaged_exit(state_dir, deadline)
    policy = cleanup_policy(len(progress.targeted), deadline=deadline)
    _deliver_all(cache_mgr, policy, bridge_url, progress)
    _fold_deferred_confirmations(cache_mgr)
    leftovers = _leftovers(cache_mgr, progress)
    if not leftovers:
        log_debug(f"--cleanup confirmed {len(progress.targeted)} session(s) Ended")
        return EXIT_OK
    if leftovers.exports:
        _export(leftovers.exports, deadline)
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
