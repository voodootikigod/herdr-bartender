"""Universal Sender Protocol post-lock dispatch (Plan §4.3 L486, L561-567; R5).

Runs after every Step C, outside the cache lock, for whatever Step C persisted:

* **Compensating Ended** (Persisted Side Effects Guarantee): budget gate, then one lock
  hold that re-verifies the entry and, when it proceeds, records the attempt BEFORE the
  POST (``sender.compensation``: ``attempts``, ``last_attempt``, ``posted``), then the
  POST, then under the lock: clear the entry and detect a re-admission that happened
  while it was in flight (force a re-sync and bump ``resync_generation``, so a sender
  still in flight for the new admission re-syncs in its Step C instead of recording a
  delivery). The re-verification aborts (clears the entry, sends nothing) when a live
  session - or a newer generation than the entry's target - now owns the session id;
  if an earlier attempt may have landed (``posted``) it forces that re-sync instead of
  a plain abort. An entry that is not yet due on its retry schedule is left alone; one
  that exhausted its attempts is journaled to the orphan file and dropped. Otherwise an
  unconfirmed entry stays owed: the event path hands it to the reconciler, whose loop
  retries it on schedule (it never re-flags ``reconciler.pending`` for it).
* **Vendor dismissals** queued by Step C: budget-gated POSTs, then one lock hold that
  purges the confirmed ones and counts the others for the reconciler's retries.

Every function returns True when the reconciler must take over; the caller performs a
single hand-off at the end.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from .. import clock
from ..bridge import DeliveryResult, send_event
from ..cache import BoundedSessionCache, CacheError, IntegrationDisabled
from ..delivery_state import force_resync_superseding_senders
from ..log import log_debug, log_warning
from ..orphans import journal_orphan_exports
from ..vendor import post_dismissal, record_dismissal_attempts
from ..watchdog import deferred_exit
from .compensation import (
    MAX_COMPENSATION_ATTEMPTS,
    compensation_wait,
    exhausted,
    find_entry,
    orphan_record,
    replace_entry,
    stamp_attempt,
    without_entry,
)
from .policy import SendPolicy
from .step_b import ensure_outside_critical_section
from .step_c import PostLock

PROCEED, ABORTED, RESYNCED, NOT_DUE, EXHAUSTED, LEFT_OWED = (
    "proceed", "aborted", "resynced", "not_due", "exhausted", "owed")


def compensation_payload(entry: dict) -> dict:
    return {"state": "Ended", "agent": entry.get("agent") or "Herdr", "session_id": entry["session_id"]}


def _supersedes(active: Optional[dict], entry: dict) -> bool:
    """A live session, or a newer generation than the compensation targets, owns the session id now."""
    if not active:
        return False
    if active.get("desired_state") != "Ended":
        return True
    target = entry.get("generation")
    return isinstance(target, int) and int(active.get("generation", 0) or 0) > target


def _owed(policy: SendPolicy) -> bool:
    """An owed entry needs a hand-off only from a sender that is not the reconciler.

    The reconciler's loop keeps owed entries as outstanding work and wakes when the next
    one is due; re-flagging ``reconciler.pending`` there would make it re-run with no sleep.
    """
    return policy.spawns_reconciler


def _spare_live_session(data: dict, stored: dict, active: dict) -> str:
    """Re-admitted: drop the entry; re-sync the live session if an earlier attempt may have landed."""
    sid = stored["session_id"]
    data["pending_compensations"] = without_entry(data.get("pending_compensations"), stored)
    if stored.get("posted") and active.get("desired_state") != "Ended":
        log_debug(f"Compensating Ended for {sid} may have landed after its re-admission; forcing re-sync")
        force_resync_superseding_senders(active)
        return RESYNCED
    log_debug(f"Aborting stale-send compensation for {sid}: re-admitted ({active.get('desired_state')}, "
              f"generation {active.get('generation')} vs target {stored.get('generation')})")
    return ABORTED


def _give_up(data: dict, stored: dict, now: float) -> str:
    """Attempts exhausted: journal the Ended for the orphan file (durable, fsynced) before dropping the entry."""
    sid = stored["session_id"]
    try:
        journal_orphan_exports([(sid, orphan_record(stored))])
    except (OSError, TypeError, ValueError) as exc:
        log_warning(f"Compensating Ended for {sid} could not be exported to the orphan file ({exc}); kept owed")
        data["pending_compensations"] = replace_entry(data.get("pending_compensations"), stored,
                                                      {**stored, "last_attempt": now})
        return NOT_DUE
    log_warning(f"Compensating Ended for {sid} unconfirmed after {MAX_COMPENSATION_ATTEMPTS} attempts; "
                "exported to the orphan file")
    data["pending_compensations"] = without_entry(data.get("pending_compensations"), stored)
    return EXHAUSTED


def _decide(data: dict, entry: dict, now: float) -> Tuple[str, Optional[dict], bool]:
    """(verdict, entry to POST, cache changed) for one owed entry, under the lock."""
    entries = data.get("pending_compensations")
    stored = find_entry(entries, entry)
    if stored is None:  # settled, or replaced by a newer compensation for the session, by another sender
        return ABORTED, None, False
    active = data.get("sessions", {}).get(stored["session_id"])
    if _supersedes(active, stored):
        return _spare_live_session(data, stored, active), None, True
    if compensation_wait(stored, now) > 0:
        return NOT_DUE, None, False
    if exhausted(stored):
        return _give_up(data, stored, now), None, True
    stamped = stamp_attempt(stored, now)
    data["pending_compensations"] = replace_entry(entries, stored, stamped)
    return PROCEED, stamped, True


def _reverify(cache_mgr: BoundedSessionCache, entry: dict) -> Tuple[str, Optional[dict]]:
    """Under the lock: re-verify ``entry`` and, when it proceeds, persist the attempt before the POST."""
    now = clock.time()
    with cache_mgr as data:
        verdict, stamped, changed = _decide(data, entry, now)
        if changed:
            cache_mgr.save(data)
    return verdict, stamped


def _settle(cache_mgr: BoundedSessionCache, entry: dict) -> bool:
    """After the POST landed: clear the entry; True (re-sync handed off) if a live session was admitted meanwhile."""
    sid = entry["session_id"]
    with cache_mgr as data:
        data["pending_compensations"] = without_entry(data.get("pending_compensations"), entry)
        active = data.get("sessions", {}).get(sid)
        raced = bool(active) and active.get("desired_state") != "Ended"
        if raced:  # a sender mid-flight for the new admission must not claim delivery: Bartender applied our Ended
            log_debug(f"Compensating Ended raced with a new admission of {sid}; forcing re-sync")
            force_resync_superseding_senders(active)
        cache_mgr.save(data)
    return raced


def _reverify_step(cache_mgr: BoundedSessionCache, entry: dict) -> Tuple[str, Optional[dict]]:
    with deferred_exit():  # a deadline here exits only after the lock is released and the decision saved
        try:
            return _reverify(cache_mgr, entry)
        except (CacheError, IntegrationDisabled) as exc:
            log_debug(f"Compensation re-verify for {entry.get('session_id')} left to the reconciler: {exc!r}")
            return LEFT_OWED, None


def _settle_step(cache_mgr: BoundedSessionCache, entry: dict, policy: SendPolicy) -> bool:
    with deferred_exit():  # clear + re-sync detection are saved before any deadline exit
        try:
            return _settle(cache_mgr, entry)
        except (CacheError, IntegrationDisabled) as exc:
            log_debug(f"Post-compensation check for {entry.get('session_id')} left to the reconciler: {exc!r}")
            return _owed(policy)


def _verdict_hand_off(verdict: str, policy: SendPolicy) -> bool:
    if verdict == RESYNCED:
        return True
    if verdict in (NOT_DUE, LEFT_OWED, EXHAUSTED):  # EXHAUSTED: the orphan export waits in the journal
        return _owed(policy)
    return False


def compensate(cache_mgr: BoundedSessionCache, entry: dict, policy: SendPolicy,
               bridge_url: Optional[str] = None) -> bool:
    """Dispatch one persisted compensating Ended; True when the reconciler must take over."""
    if not policy.allows_network():
        log_debug(f"Budget spent; compensation for {entry.get('session_id')} left to the reconciler")
        return True
    verdict, stamped = _reverify_step(cache_mgr, entry)
    if verdict != PROCEED or stamped is None:
        return _verdict_hand_off(verdict, policy)
    ensure_outside_critical_section()
    result = send_event(compensation_payload(stamped), timeout=policy.socket_timeout(), bridge_url=bridge_url)
    if not result.success:
        log_debug(f"Compensating Ended for {stamped['session_id']} not confirmed ({result.error}); "
                  f"attempt {stamped['attempts']} of {MAX_COMPENSATION_ATTEMPTS}, left owed")
        return _owed(policy)
    return _settle_step(cache_mgr, stamped, policy)


def dismiss_vendors(cache_mgr: BoundedSessionCache, uuids: Sequence[str], policy: SendPolicy,
                    bridge_url: Optional[str] = None) -> bool:
    """Send the queued vendor dismissals (budget-gated) and record them; True when any is left owed."""
    sent: List[Tuple[str, DeliveryResult]] = []
    for uuid in uuids:
        if not policy.allows_network():
            log_debug(f"Budget spent; vendor dismissal of {uuid} left queued for the reconciler")
            break
        ensure_outside_critical_section()
        sent.append((uuid, post_dismissal(uuid, timeout=policy.socket_timeout(), bridge_url=bridge_url)))
    recorded = record_dismissal_attempts(cache_mgr, sent)
    confirmed = sum(1 for _, result in sent if result.success)
    return not recorded or confirmed < len(uuids)


def run_post_lock(cache_mgr: BoundedSessionCache, post: PostLock, policy: SendPolicy,
                  bridge_url: Optional[str] = None) -> bool:
    """All post-lock work for one Step C; True when the reconciler must take over."""
    need = post.hand_off
    for entry in post.compensations:
        need = compensate(cache_mgr, entry, policy, bridge_url) or need
    if post.dismissals:
        need = dismiss_vendors(cache_mgr, post.dismissals, policy, bridge_url) or need
    return need
