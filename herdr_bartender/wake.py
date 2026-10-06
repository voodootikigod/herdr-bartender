"""Reconciler wake-ups (Plan §5.1 item 3): ``reconciler.pending``, spool/results arrivals and the 0.5s sleep ticks.

* Only regular ``*.json`` files in ``spool/`` and ``results/`` count as envelopes - exactly what
  their consumers process (a directory named ``x.json`` is not an envelope).
* An envelope a full pass could not consume - present before and after a pass that consumed
  nothing, e.g. one that can be neither unlinked nor quarantined (EACCES/EPERM) - is *stuck*:
  it no longer wakes the loop nor counts as work (the consumers still retry it on every pass),
  so it can never turn the loop into a busy loop or keep it from idling out. It is logged once.
"""

from __future__ import annotations

from pathlib import Path
from typing import FrozenSet

from . import clock
from .log import log_debug, log_warning
from .markers import is_disabled

ENVELOPE_DIRS = ("spool", "results")
WAKE_TICK_SECONDS = 0.5
MIN_TICK_SECONDS = 0.001

Envelopes = FrozenSet[str]   # "spool/<name>", "results/<name>"


def envelope_files(state_dir: Path) -> Envelopes:
    """The regular ``*.json`` envelopes waiting in spool/ and results/."""
    names = []
    for sub in ENVELOPE_DIRS:
        directory = state_dir / sub
        try:
            if directory.is_dir():
                names.extend(f"{sub}/{path.name}" for path in directory.glob("*.json") if path.is_file())
        except OSError as exc:
            log_debug(f"Could not list {directory}: {exc}")
    return frozenset(names)


def envelopes_waiting(state_dir: Path, stuck: Envelopes = frozenset()) -> bool:
    """Spool events and/or deferred delivery results still to be applied (stuck ones excluded)."""
    return bool(envelope_files(state_dir) - stuck)


def still_stuck(before: Envelopes, after: Envelopes, stuck: Envelopes, full: bool) -> Envelopes:
    """The stuck set after a pass: entries still present; plus, after a full pass that consumed nothing, every
    envelope it left behind (envelopes that arrived during the pass are never stuck)."""
    kept = stuck & after
    if not full or not before or before - after:
        return kept
    new = (before & after) - stuck
    if new:
        log_warning(f"Reconciler cannot consume {sorted(new)}; retried each pass, no longer a wake-up")
    return kept | new


def wake_requested(state_dir: Path, pending_name: str, stuck: Envelopes = frozenset()) -> bool:
    """Plan §5.1 item 3: a pending touch or a spool/results arrival breaks the sleep."""
    return (state_dir / pending_name).exists() or envelopes_waiting(state_dir, stuck)


def sleep_until(wall_deadline: float, state_dir: Path, pending_name: str, stuck: Envelopes = frozenset()) -> None:
    """Sleep in 0.5s ticks (monotonic) until ``wall_deadline``; a pending touch or an envelope ends it early."""
    end = clock.monotonic() + max(0.0, wall_deadline - clock.time())
    while not is_disabled() and not wake_requested(state_dir, pending_name, stuck):
        remaining = end - clock.monotonic()
        if remaining <= 0:
            return
        clock.sleep(max(MIN_TICK_SECONDS, min(WAKE_TICK_SECONDS, remaining)))


def consume_pending(pending_file: Path) -> bool:
    """Unlink ``reconciler.pending`` right before a pass; True when it was set."""
    try:
        pending_file.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        log_debug(f"Could not consume {pending_file.name}: {exc}")
        return pending_file.exists()
