"""Persisted compensating-Ended entries: identity, retry schedule, exhaustion (Plan §4.3, §3.3).

``delivery_state.stage_compensation`` creates a ``pending_compensations`` entry from
identity fields (session id, pane, agent, target generation, ``admitted_at_ns``,
timestamp). The dispatch (``sender.dispatch.compensate``) adds bookkeeping under the
re-verification lock, right BEFORE each POST:

* ``attempts`` / ``last_attempt``: attempt n+1 is due ``COMPENSATION_RETRY_DELAYS[n]``
  seconds after attempt n (0, 1, 2, 4, 8s). Once ``MAX_COMPENSATION_ATTEMPTS`` POSTs went
  unconfirmed (and the last delay passed), the Ended is exported to the orphan file
  (replayed when the bridge is healthy) and the entry is dropped.
* ``posted``: a compensating Ended for this session may have reached Bartender (a
  timeout, a 5xx, a failed settle or a deadline exit can still land it). A live session
  found later is then re-synced, not merely spared.

The reconciler loop treats owed entries as outstanding work and sleeps until the next
one is due, so an unconfirmed entry never re-flags ``reconciler.pending`` (no busy loop).

Pure functions only: no I/O, no lock.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional

COMPENSATION_RETRY_DELAYS = (0.0, 1.0, 2.0, 4.0, 8.0)
MAX_COMPENSATION_ATTEMPTS = len(COMPENSATION_RETRY_DELAYS)
BOOKKEEPING_FIELDS = ("attempts", "last_attempt", "posted")


def valid_entry(entry: object) -> bool:
    return isinstance(entry, dict) and isinstance(entry.get("session_id"), str) and bool(entry["session_id"])


def _identity(entry: Mapping) -> dict:
    return {k: v for k, v in entry.items() if k not in BOOKKEEPING_FIELDS}


def same_entry(a: object, b: object) -> bool:
    """The same staged compensation, whatever bookkeeping either copy carries."""
    return isinstance(a, dict) and isinstance(b, dict) and _identity(a) == _identity(b)


def _as_list(entries: object) -> list:
    return entries if isinstance(entries, list) else []


def find_entry(entries: object, entry: Mapping) -> Optional[dict]:
    return next((c for c in _as_list(entries) if same_entry(c, entry)), None)


def without_entry(entries: object, entry: Mapping) -> list:
    return [c for c in _as_list(entries) if not same_entry(c, entry)]


def replace_entry(entries: object, entry: Mapping, updated: dict) -> list:
    return [updated if same_entry(c, entry) else c for c in _as_list(entries)]


def attempts_of(entry: Mapping) -> int:
    value = entry.get("attempts", 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def exhausted(entry: Mapping) -> bool:
    return attempts_of(entry) >= MAX_COMPENSATION_ATTEMPTS


def stamp_attempt(entry: Mapping, now: float) -> dict:
    """A copy recording one more POST about to be made (and that it may land)."""
    return {**entry, "attempts": attempts_of(entry) + 1, "last_attempt": now, "posted": True}


def compensation_wait(entry: Mapping, now: float) -> float:
    """Seconds until ``entry`` is due (its next POST, or its export once exhausted); 0.0 when due now."""
    attempts = attempts_of(entry)
    if attempts == 0:
        return 0.0
    last = entry.get("last_attempt")
    if isinstance(last, bool) or not isinstance(last, (int, float)):
        return 0.0
    elapsed = now - float(last)
    if elapsed < 0:  # the wall clock stepped back: never stall the entry until it catches up
        return 0.0
    delay = COMPENSATION_RETRY_DELAYS[min(attempts, MAX_COMPENSATION_ATTEMPTS - 1)]
    return max(0.0, delay - elapsed)


def next_compensation_wait(entries: Iterable[object], now: float) -> Optional[float]:
    """Seconds until the earliest owed entry is due; None when no (well-formed) entry is owed."""
    waits = [compensation_wait(e, now) for e in entries if valid_entry(e)]
    return min(waits) if waits else None


def orphan_record(entry: Mapping) -> dict:
    """The orphan-file record for an exhausted compensation (replayed as a minimal Ended)."""
    return {
        "pane_id": entry.get("pane_id"),
        "agent": entry.get("agent") or "Herdr",
        "generation": entry.get("generation"),
        "admitted_at_ns": entry.get("admitted_at_ns"),
        "desired_state": "Ended",
        "orphaned_ended": True,
        "compensation": True,
        "last_event_at": entry.get("timestamp"),
    }
