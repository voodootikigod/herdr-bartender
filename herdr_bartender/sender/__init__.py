"""The Universal Sender Protocol (Plan §4.3, §5.1 item 1; R5, R9, R10, R14, R23).

One implementation of Step A (lease evaluation + claim under the cache lock), Step B
(capped, budget-gated network I/O outside the lock), Step C (apply the outcome under
the lock through ``delivery_state.apply_delivery_result``) and the post-lock dispatch
(compensations, vendor dismissals, orphan I/O, reconciler hand-off) for every sender:
the event handlers use ``EVENT_POLICY``; the background reconciler reuses the same
functions with ``BACKGROUND_POLICY`` (no 1.5s event deadline).

Typical use::

    result = run_step_a(cache_mgr, stage, policy=policy, arrival_ns=arr_ns)
    hand_off = result.hand_off
    for claim in result.claims:
        report = deliver_claim(cache_mgr, claim, policy=policy, bridge_url=url)
        hand_off = hand_off or report.hand_off
    finish(policy, hand_off=hand_off, live_remaining=...)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..cache import BoundedSessionCache
from .dispatch import compensate, dismiss_vendors, run_post_lock
from .lease import (
    LEASE_GRACE_SECONDS,
    LEASE_SECONDS,
    Claim,
    claim_lease,
    lease_active_elsewhere,
    release_lease,
    warm_step_a_probes,
)
from .policy import BACKGROUND_POLICY, EVENT_POLICY, SendPolicy, cleanup_budget, cleanup_policy
from .step_a import (
    ClaimPlan,
    Stage,
    Staged,
    StepAResult,
    Target,
    claim_targets,
    has_live_sessions,
    has_owed_work,
    run_step_a,
)
from .step_b import CriticalSectionViolation, Sent, ensure_outside_critical_section, transmit
from .step_c import PostLock, settle


@dataclass(frozen=True)
class DeliveryReport:
    hand_off: bool                    # the reconciler must take over
    live_remaining: Optional[bool]    # None: Step C did not run


def deliver_claim(cache_mgr: BoundedSessionCache, claim: Claim, *, policy: SendPolicy = EVENT_POLICY,
                  bridge_url: Optional[str] = None) -> DeliveryReport:
    """Steps B and C plus the post-lock dispatch for one claimed session.

    The dispatch ALWAYS runs after Step C (an empty ``PostLock`` makes it a no-op); the
    caller performs the single reconciler hand-off (``finish``).
    """
    sent = transmit(claim, policy, bridge_url)
    post = settle(cache_mgr, claim, sent, policy)
    hand_off = run_post_lock(cache_mgr, post, policy, bridge_url)
    return DeliveryReport(hand_off, post.live_remaining)


def finish(policy: SendPolicy, *, hand_off: bool, live_remaining: bool) -> None:
    """One hand-off per run, else the Plan §4.3 L568 unconditional watchdog check."""
    if hand_off:
        policy.hand_off()
    elif live_remaining:
        policy.ensure_watchdog()


__all__ = [
    "BACKGROUND_POLICY", "Claim", "ClaimPlan", "CriticalSectionViolation", "DeliveryReport", "EVENT_POLICY",
    "LEASE_GRACE_SECONDS", "LEASE_SECONDS", "PostLock", "SendPolicy", "Sent", "Stage", "Staged", "StepAResult", "Target",
    "claim_lease", "claim_targets", "cleanup_budget", "cleanup_policy", "compensate", "deliver_claim",
    "dismiss_vendors",
    "ensure_outside_critical_section", "finish", "has_live_sessions", "has_owed_work", "lease_active_elsewhere", "release_lease",
    "run_post_lock", "run_step_a", "settle", "transmit", "warm_step_a_probes",
]
