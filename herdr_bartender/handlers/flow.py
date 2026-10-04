"""The event-path flow shared by every handler (Plan §4.3 items 1-5).

1. ``prepare()`` runs the handler's intake (identity, admission prerequisites) and
   returns the pure staging function for Step A, or None to ignore the event.
2. Step A (``sender.run_step_a``) under one lock hold. If the lock cannot be taken or
   the save fails, the event is spooled instead (``spool.defer_event``) and nothing is
   sent. Intake, Step A, the spool fallback and the fold of the orphan journal (records
   pruned at the 256 cap, journaled by Step A before its save) run inside
   ``watchdog.deferred_exit()``, so a deadline cannot drop the event (R23).
3. For each claimed session (at most one, R5): ``sender.deliver_claim`` (Step B,
   Step C, post-lock dispatch).
4. The vendor dismissals Step A queued (pane close) are sent, budget-gated.
5. Exactly one reconciler hand-off when anything is left over, else the unconditional
   watchdog check (a live session keeps the reconciler's heartbeat running). An event
   ignored at intake still gets the watchdog check; an unexpected error after Step A
   still hands off (then propagates), so a bug degrades to a reconciler retry.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Optional, Tuple

from .. import clock
from ..cache import BoundedSessionCache, CacheError, IntegrationDisabled
from ..log import log_debug, log_warning
from ..orphans import flush_pending_orphan_ops
from ..paths import get_state_dir
from ..sender import (
    EVENT_POLICY,
    SendPolicy,
    Stage,
    StepAResult,
    deliver_claim,
    dismiss_vendors,
    finish,
    has_live_sessions,
    run_step_a,
    warm_step_a_probes,
)
from ..sender.lease import peek_cache
from ..spool import defer_event
from ..watchdog import deferred_exit

Prepare = Callable[[], Optional[Stage]]


def _watchdog_check_without_step_a(policy: SendPolicy) -> None:
    """Plan §4.3 L568 for an event ignored at intake: ensure the reconciler while a live session is cached.

    The lock-free peek only decides whether to make sure the (singleton) reconciler runs;
    no cache state is changed from it.
    """
    if has_live_sessions(peek_cache(get_state_dir())):
        policy.ensure_watchdog()


def _fold_pruned_records(result: StepAResult, policy: SendPolicy) -> StepAResult:
    """Fold the orphan journal Step A wrote for pruned records into the orphan file (R10 lock mode)."""
    if not result.orphan_exports or flush_pending_orphan_ops(blocking=policy.orphan_blocking):
        return result
    log_debug("Orphan lock contended; pruned records stay journaled for the reconciler")
    return replace(result, hand_off=True)


def _step_a_or_spool(event_name: str, event_data: dict, context: dict, arr_ns: int, prepare: Prepare,
                     policy: SendPolicy) -> Optional[Tuple[BoundedSessionCache, StepAResult]]:
    """Intake and Step A, or the spool when the lock/save fails; None when nothing more is to be done."""
    stage = prepare()
    if stage is None:
        _watchdog_check_without_step_a(policy)
        return None
    state_dir = get_state_dir()
    cache_mgr = policy.cache(state_dir)
    warm_step_a_probes(state_dir, clock.time())  # liveness facts resolved before the lock (Plan §6.1)
    try:
        result = run_step_a(cache_mgr, stage, policy=policy, arrival_ns=arr_ns)
    except IntegrationDisabled:
        return None
    except CacheError as exc:  # contention or unsaved state: never proceed, never send
        defer_event(event_name, event_data, context, arr_ns, exc)
        return None
    return cache_mgr, _fold_pruned_records(result, policy)


def _deliver(cache_mgr: BoundedSessionCache, result: StepAResult, policy: SendPolicy,
             bridge_url: Optional[str]) -> Tuple[bool, bool]:
    """Steps B/C and the post-lock dispatch for the claims, then the queued dismissals: (hand_off, live)."""
    hand_off, live = result.hand_off, result.live_remaining
    for claim in result.claims:
        report = deliver_claim(cache_mgr, claim, policy=policy, bridge_url=bridge_url)
        hand_off = hand_off or report.hand_off
        if report.live_remaining is not None:
            live = report.live_remaining
    if result.dismissals:
        hand_off = dismiss_vendors(cache_mgr, result.dismissals, policy, bridge_url) or hand_off
    return hand_off, live


def run_event(event_name: str, event_data: dict, context: dict, arr_ns: int, prepare: Prepare,
              bridge_url: Optional[str] = None, policy: SendPolicy = EVENT_POLICY) -> None:
    with deferred_exit():  # Plan §4.3 contention rule under R23: saved or spooled before any deadline exit
        step = _step_a_or_spool(event_name, event_data, context, arr_ns, prepare, policy)
    if step is None:
        return
    cache_mgr, result = step
    try:
        hand_off, live = _deliver(cache_mgr, result, policy, bridge_url)
    except Exception as exc:
        log_warning(f"{event_name} failed after Step A saved it ({exc!r}); handing off to the reconciler")
        policy.hand_off()
        raise
    finish(policy, hand_off=hand_off, live_remaining=live)
