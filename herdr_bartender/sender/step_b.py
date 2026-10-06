"""Universal Sender Protocol Step B: network I/O outside the cache lock (Plan §4.3 item 3, §3.3, R5).

At most ``policy.max_posts`` POSTs per claimed session: the primary POST, plus ONE
minimal ``{"state": "Ended", "agent": "Herdr", "session_id": sid}`` retry when an
``Ended`` failed or was rejected (§4.3 L479, §1 L119, R45): every unsuccessful outcome
except a request that never reached the bridge (Bartender not running, invalid bridge
URL). Every POST is gated on the policy budget (``time_remaining() > 0.3`` on the event
path) and uses the policy socket timeout (``min(0.2, max(0.05, time_remaining() - 0.3))``).
Sending while the cache lock is held is a programming error and raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .. import runtime
from ..bridge import (ERR_BARTENDER_NOT_RUNNING, ERR_INVALID_BRIDGE_URL, CriticalSectionViolation, DeliveryResult,
                      minimal_ended_payload, send_event)
from ..log import log_debug
from .lease import Claim
from .policy import SendPolicy

NOT_SENT_BUDGET = "budget"
UNSENT_ERRORS = (ERR_BARTENDER_NOT_RUNNING, ERR_INVALID_BRIDGE_URL)   # never reached the bridge: a retry fixes nothing


@dataclass(frozen=True)
class Sent:
    """What Step B did for one claim: the final classified result, or None when nothing was sent."""

    result: Optional[DeliveryResult]
    posts: int = 0
    skipped: Optional[str] = None   # why nothing was sent (NOT_SENT_BUDGET)

    @property
    def transmitted(self) -> bool:
        return self.result is not None


def ensure_outside_critical_section() -> None:
    """``assert not IN_CRITICAL_SECTION`` (Plan §1 L9), enforced in every mode."""
    if runtime.IN_CRITICAL_SECTION:
        log_debug("FATAL: network I/O attempted while holding the cache lock")
        raise CriticalSectionViolation("network I/O attempted while holding the cache lock")


def _wants_minimal_retry(claim: Claim, result: DeliveryResult) -> bool:
    """§4.3 L479 / R45: an Ended that failed or was rejected is retried once with the minimal payload."""
    if claim.state != "Ended" or result.success or result.error in UNSENT_ERRORS:
        return False
    return minimal_ended_payload(claim.payload) != claim.payload


def transmit(claim: Claim, policy: SendPolicy, bridge_url: Optional[str] = None) -> Sent:
    """Step B for one claimed session (R5 cap: at most ``policy.max_posts`` POSTs)."""
    ensure_outside_critical_section()
    if not policy.allows_network():
        log_debug(f"Budget spent before sending {claim.session_id} seq {claim.seq}; left to the reconciler")
        return Sent(None, 0, NOT_SENT_BUDGET)
    result = send_event(claim.payload, timeout=policy.socket_timeout(), bridge_url=bridge_url)
    if policy.max_posts < 2 or not _wants_minimal_retry(claim, result) or not policy.allows_network():
        return Sent(result, 1)
    log_debug(f"Retrying Ended for {claim.session_id} with the minimal payload after {result.error}")
    retry = send_event(minimal_ended_payload(claim.payload), timeout=policy.socket_timeout(), bridge_url=bridge_url)
    return Sent(retry, 2)
