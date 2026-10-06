"""Universal Sender Protocol Step C: apply the outcome under the cache lock (Plan §4.3 item 4).

The decision is ``delivery_state.apply_delivery_result`` (shared with the results drain):
evicted session -> compensation; tombstoned non-Ended send -> compensation or abort plus
lease release; stale; lease mismatch -> ``resync_generation`` bump, ``delivered_seq = 0``,
``in_flight`` and reconciler hand-off; then the §3.3 response matrix with the success
staleness guard; a STALE verdict still releases the claimant's own lease. The lease is
never kept for a follow-up send (R5): a newer seq that arrived during the send releases
the lease and hands off to the reconciler.

The outcome's DELIVERY_DOWN transition is committed only after the Step C save succeeds, so
a reconnection whose re-sync is written to ``results/`` instead keeps the flag for the drain.

Before the lock is released, every side effect owed outside it is persisted
(``pending_compensations`` with target generation and ``admitted_at_ns``; vendor
dismissals queued in ``dismissed_vendor_uuids``). A Step C that cannot lock or save writes
a ``results/`` envelope instead; a claim Step B could not send (budget) only releases its
lease. Step C and its orphan I/O run inside ``watchdog.deferred_exit()``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional, Tuple

from .. import clock
from ..cache import BoundedSessionCache, CacheError, IntegrationDisabled
from ..delivery_state import (
    MISSING,
    STALE,
    Outcome,
    StagedEffects,
    apply_delivery_result,
    commit_delivery_down,
    journal_owed_exports,
    run_orphan_effects,
)
from ..log import log_debug, log_warning
from ..results import ResultWriteError, write_result_envelope
from ..vendor import resolve_vendor_cleanups, warm_fallback_probe
from ..watchdog import deferred_exit
from .lease import Claim, owns_lease, release_lease
from .policy import SendPolicy
from .step_a import has_live_sessions, has_owed_root_work
from .step_b import Sent

UNSENT = "unsent"
NO_CHANGE_VERDICTS = (MISSING, STALE)   # apply_delivery_result left the cache untouched


@dataclass(frozen=True)
class PostLock:
    """Work Step C left for after the lock is released (always dispatched, never skipped)."""

    compensations: Tuple[dict, ...] = ()   # persisted pending_compensations entries owed now
    dismissals: Tuple[str, ...] = ()       # vendor UUIDs queued under the lock, to send now
    hand_off: bool = False                 # the reconciler must take over (flag + ensure)
    live_remaining: Optional[bool] = None  # None: Step C did not run


def release_unsent(data: dict, claim: Claim) -> StagedEffects:
    """Step B sent nothing (budget): release our lease and leave the seq to the reconciler."""
    session = data.get("sessions", {}).get(claim.session_id)
    if owns_lease(session, claim.token):
        release_lease(session)
    pending = bool(session) and int(session.get("delivered_seq", 0) or 0) < int(session.get("seq", 0) or 0)
    return StagedEffects(UNSENT, touch_pending=pending, spawn_reconciler=pending)


def release_after_stale(data: dict, claim: Claim, effects: StagedEffects) -> Tuple[StagedEffects, bool]:
    """A STALE verdict changes nothing else, but our own lease must not outlive Step C.

    Otherwise a long-lived holder (the reconciler) would make other senders defer to it
    until deadline + grace. Returns (effects, cache changed); a seq still owed once our
    lease is gone is flagged for the reconciler.
    """
    session = data.get("sessions", {}).get(claim.session_id)
    if effects.verdict != STALE or not owns_lease(session, claim.token):
        return effects, False
    release_lease(session)
    pending = int(session.get("delivered_seq", 0) or 0) < int(session.get("seq", 0) or 0)
    return replace(effects, touch_pending=effects.touch_pending or pending,
                   spawn_reconciler=effects.spawn_reconciler or pending), True


def _apply(data: dict, claim: Claim, sent: Sent, now: float, now_ns: int) -> Tuple[StagedEffects, bool]:
    """(effects, cache changed)."""
    if not sent.transmitted:
        return release_unsent(data, claim), True
    effects = apply_delivery_result(data, claim.transmission(), Outcome.from_delivery(sent.result),
                                    now=now, now_ns=now_ns)
    if effects.verdict in NO_CHANGE_VERDICTS:
        return release_after_stale(data, claim, effects)
    return effects, True


def _vendor_panes(effects: StagedEffects) -> Tuple[str, ...]:
    return tuple(entry["pane_id"] for entry in effects.vendor_cleanups if entry.get("pane_id"))


def _settle_locked(cache_mgr: BoundedSessionCache, claim: Claim, sent: Sent
                   ) -> Tuple[PostLock, StagedEffects, bool]:
    """(post-lock work, effects, owed orphan exports already journaled before the save)."""
    now, now_ns = clock.time(), clock.time_ns()
    journaled = False
    with cache_mgr as data:
        effects, changed = _apply(data, claim, sent, now, now_ns)
        vendor = resolve_vendor_cleanups(data, _vendor_panes(effects), now)
        live = has_live_sessions(data) or has_owed_root_work(data)   # R78: owed work keeps the watchdog too
        if changed or vendor.changed:
            journaled = journal_owed_exports(effects, data)   # durable before the save marks them orphaned_ended
            cache_mgr.save(data)
            commit_delivery_down(effects)   # a reconnection clears DELIVERY_DOWN only once its re-sync is saved
    vendor.commit()   # R88: vendor-file I/O only after the lock is released (and only after the save)
    # R52: an owed export that could not be journaled leaves the record flagged; a reconciler pass re-mirrors it.
    unmirrored = bool(effects.orphans_to_export) and not journaled
    post = PostLock(effects.compensations, vendor.dismissals,
                    effects.touch_pending or effects.spawn_reconciler or unmirrored, live)
    return post, effects, journaled


def _deferred(claim: Claim, sent: Sent, reason: object) -> PostLock:
    """Step C could not lock or save: persist a sent outcome in results/ and hand off."""
    if not sent.transmitted:
        log_debug(f"Step C for unsent {claim.session_id} deferred ({reason}); the lease lapses for the reconciler")
        return PostLock(hand_off=True)
    try:
        path = write_result_envelope(claim.transmission(), Outcome.from_delivery(sent.result))
        log_debug(f"Step C deferred for {claim.session_id} seq {claim.seq} ({reason}); wrote {path.name}")
    except ResultWriteError as exc:
        log_warning(f"Step C result for {claim.session_id} seq {claim.seq} lost ({reason}): {exc}")
    return PostLock(hand_off=True)


def settle(cache_mgr: BoundedSessionCache, claim: Claim, sent: Sent, policy: SendPolicy) -> PostLock:
    """Step C for one claim; never raises CacheError (falls back to results/)."""
    if not policy.bounded:
        warm_fallback_probe()   # R35: the reconciler / --cleanup may confirm a delivery while Herdr is dead
    with deferred_exit():  # the outcome is applied (with its orphan I/O) or written to results/ first
        try:
            post, effects, journaled = _settle_locked(cache_mgr, claim, sent)
        except IntegrationDisabled:
            log_debug(f"DISABLED under the Step C lock; dropping the outcome for {claim.session_id}")
            return PostLock()
        except CacheError as exc:
            return _deferred(claim, sent, exc)
        run_orphan_effects(effects, blocking=policy.orphan_blocking, journaled=journaled)
    return post
