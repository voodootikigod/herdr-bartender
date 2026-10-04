"""Universal Sender Protocol Step A: one critical section under the cache lock (Plan §4.3 item 2, item 5).

``run_step_a`` takes the lock once and, in this order: replays the spool, applies the
caller's pure staging function, evaluates every staged session's lease with the 6-row
truth table (``lease.lease_active_elsewhere``; liveness read from the pre-lock memo only),
claims at most ``policy.max_sessions`` leases, clears the leases of the overflow sessions
(left ``in_flight`` for the reconciler), resolves every persisted ``pending_vendor_cleanups``
entry (a pane close stages one; queued dismissals are sent after the lock), saves, and
only then unlinks replayed envelopes and resolved ``.vendor_active`` files.

No network I/O, no subprocess and no orphan-file I/O happen under the lock. The one
extra local write is the orphan JOURNAL entry (no orphan lock) of each undelivered Ended
pruned at the 256 cap, fsynced before the save that prunes it, so the owed Ended survives
a crash right after that save (a journal failure aborts Step A: nothing is saved and the
event is spooled). The caller folds the journal into the orphan file, delivers, and
performs the single reconciler hand-off after the lock is released.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Tuple

from .. import clock
from ..cache import BoundedSessionCache, CacheError
from ..orphans import journal_orphan_exports
from ..process import memoised_herdr_alive, memoised_instance_alive
from ..spool import replay_spool_locked
from ..vendor import pending_vendor_panes, resolve_vendor_cleanups
from .lease import Claim, InstanceAlive, claim_lease, lease_active_elsewhere, release_lease
from .policy import SendPolicy

OrphanExports = Tuple[Tuple[str, dict], ...]


@dataclass(frozen=True)
class Target:
    """A session the staging left with an undelivered seq."""

    session_id: str
    pane_id: Optional[str]
    record: dict


@dataclass(frozen=True)
class Staged:
    """What a handler's pure staging function did to the cache under the Step A lock."""

    targets: Tuple[Target, ...] = ()
    mutated: bool = False                  # the cache changed (save it)
    orphan_exports: OrphanExports = ()     # undelivered Ended records pruned at the 256 cap (journaled in Step A)


Stage = Callable[[dict], Staged]


class OrphanMirrorError(CacheError):
    """An undelivered Ended pruned at the 256 cap could not be journaled; nothing was saved."""


@dataclass(frozen=True)
class ClaimPlan:
    claims: Tuple[Claim, ...]
    deferred: bool    # a live foreign sender holds a lease (truth table rows 4/5)
    overflow: bool    # more sessions than the policy may deliver now (cascade clamp)


@dataclass(frozen=True)
class StepAResult:
    claims: Tuple[Claim, ...] = ()
    hand_off: bool = False               # flag + ensure the reconciler after the lock
    live_remaining: bool = False         # a non-Ended session remains (watchdog check)
    orphan_exports: OrphanExports = ()
    dismissals: Tuple[str, ...] = ()     # vendor UUIDs queued under the lock, to send now


def has_live_sessions(data: dict) -> bool:
    """Plan §4.3 L568: any cached session whose desired state is not Ended."""
    return any(isinstance(rec, dict) and rec.get("desired_state") != "Ended"
               for rec in data.get("sessions", {}).values())


def claim_targets(targets: Sequence[Target], now_wall: float, limit: Optional[int], arrival_ns: Optional[int],
                  instance_alive: InstanceAlive = memoised_instance_alive) -> ClaimPlan:
    """Lease evaluation for each target (under the lock): claim, defer, or release as overflow."""
    claims, deferred, overflow = [], False, False
    for target in targets:
        if lease_active_elsewhere(target.record, now_wall, instance_alive):
            deferred = True
            continue
        if limit is not None and len(claims) >= limit:
            release_lease(target.record)              # same transaction as the staging (gap cascade-overflow)
            target.record["delivery_status"] = "in_flight"
            overflow = True
            continue
        claims.append(claim_lease(target.session_id, target.pane_id, target.record, now_wall, arrival_ns))
    return ClaimPlan(tuple(claims), deferred, overflow)


def mirror_pruned_records(exports: OrphanExports) -> None:
    """Journal the pruned undelivered Ended records before the save that prunes them (under the lock)."""
    if not exports:
        return
    try:
        journal_orphan_exports(exports)
    except (OSError, TypeError, ValueError) as exc:
        raise OrphanMirrorError(f"could not journal {len(exports)} Ended record(s) pruned at the cap: {exc}") from exc


def run_step_a(cache_mgr: BoundedSessionCache, stage: Stage, *, policy: SendPolicy, arrival_ns: Optional[int],
               herdr_alive: Callable[[], bool] = memoised_herdr_alive) -> StepAResult:
    """One Step A transaction; raises CacheError (nothing saved) or IntegrationDisabled."""
    with cache_mgr as data:
        batch = replay_spool_locked(data, herdr_alive=herdr_alive)
        staged = stage(data)
        mirror_pruned_records(staged.orphan_exports)
        plan = claim_targets(staged.targets, clock.time(), policy.max_sessions, arrival_ns)
        vendor = resolve_vendor_cleanups(data, pending_vendor_panes(data), clock.time())
        if batch.needs_save or staged.mutated or vendor.changed:
            cache_mgr.save(data)
            batch.commit()
        vendor.commit()  # only once the queued dismissals are saved (a failed save raised above)
        live = has_live_sessions(data)
    hand_off = bool(batch.staged_sessions) or plan.deferred or plan.overflow
    return StepAResult(plan.claims, hand_off, live, staged.orphan_exports, vendor.dismissals)
