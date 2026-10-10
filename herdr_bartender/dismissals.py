"""Reconciler side of the vendor dismissal queue (Plan §5.1 item 5b, §1 L43/L56; R11, R19).

``dismissed_vendor_uuids`` entries are staged under the cache lock before any send
(``vendor.stage_dismissal``). The reconciler owns everything after the first attempt:

* **Cancellation** (Plan §5.1 5b): an entry is purged WITHOUT sending when the
  integration is disabled (``DISABLED`` or ``NO_HOOKS``), Herdr is dead, or vendor
  fallback is active on its pane (``<hex>.vendor_active`` or ``<hex>.failed`` exists).
  The pane-scoped triggers protect a live vendor fallback representation on that
  pane; a dismissal queued by a pane/tab/workspace CLOSE (``pane_closed``) has no such
  pane left (the vendor CLI died with it), so only the global triggers cancel it -
  otherwise a close whose own Ended was rejected (``.failed``) would strand the vendor
  entry (R24). Any other confirmed Ended (TTL, Herdr dead/restart, agent exit,
  --cleanup) leaves the pane alive and stays subject to the pane-scoped triggers.
* **Cadence** (R11): sends at t=0, 2, 4, 6, 8 - an entry is due when it was never
  attempted or its last attempt is 2.0s old; it is purged on HTTP 200, at its 5th
  attempt (``vendor.record_dismissal_attempt``) or once it is 10s old. An entry
  without a usable timestamp is stamped now (the 60s hard cap of R11 is thereby
  subsumed by the 10s window).
* **12h vendor horizon** (§5.1 item 5b): a UUID-bearing ``.vendor_active`` untouched
  for 12h is dismissed and retired like ``cleanup_vendor_active``; bare touches are
  never swept on mtime, and nothing is swept while Herdr is dead.

All file probes and the POSTs happen outside the cache lock; the lock holds only the
queue bookkeeping (and the reads of the ``.vendor_active`` files being retired).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Tuple

from . import clock
from .cache import BoundedSessionCache
from .config import VENDOR_UUID_REGEX
from .log import log_debug
from .vendor import (
    VENDOR_ACTIVE_SUFFIX,
    DISMISSAL_MAX_ATTEMPTS,
    DISMISSAL_WINDOW_SECONDS,
    VendorFile,
    cap_dismissals,
    parse_vendor_uuid,
    parse_vendor_uuids,
    read_vendor_file,
    retire_vendor_file,
    stage_dismissal_hex,
)

DISMISSAL_INTERVAL_SECONDS = 2.0
VENDOR_ACTIVE_HORIZON_SECONDS = 43200.0
DUE_TOLERANCE_SECONDS = 1e-3   # wall-clock float rounding must not push a due entry past its tick

PURGE, CANCEL, DUE, WAIT = "purge", "cancel", "due", "wait"
Probe = Callable[[str], bool]   # pane_hex -> vendor fallback active on that pane


@dataclass(frozen=True)
class Triggers:
    """Cancellation inputs resolved outside the lock (file checks; Herdr from the pass snapshot)."""

    disabled: bool = False
    no_hooks: bool = False
    herdr_dead: bool = False
    fallback_active: Probe = lambda _hex: False

    @property
    def global_cancel(self) -> bool:
        return self.disabled or self.no_hooks or self.herdr_dead


def _number(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _attempts(entry: dict) -> int:
    value = _number(entry.get("attempts"))
    return int(value) if value is not None and value > 0 else 0


def pane_fallback_probe(state_dir: Path) -> Probe:
    panes = Path(state_dir) / "panes"

    def active(pane_hex: str) -> bool:
        if not pane_hex:
            return False
        return (panes / f"{pane_hex}{VENDOR_ACTIVE_SUFFIX}").exists() or (panes / f"{pane_hex}.failed").exists()
    return active


def triggers_for(state_dir: Path, herdr_alive: bool) -> Triggers:
    state_dir = Path(state_dir)
    return Triggers(disabled=(state_dir / "DISABLED").exists(), no_hooks=(state_dir / "NO_HOOKS").exists(),
                    herdr_dead=not herdr_alive, fallback_active=pane_fallback_probe(state_dir))


def cancelled(entry: dict, triggers: Triggers) -> bool:
    if triggers.global_cancel:
        return True
    if entry.get("pane_closed"):
        return False
    return triggers.fallback_active(str(entry.get("pane_hex") or ""))


def verdict(uuid: object, entry: object, now: float, triggers: Triggers) -> str:
    """PURGE (malformed, too old, attempts spent), CANCEL, DUE (send now) or WAIT."""
    if not isinstance(uuid, str) or not VENDOR_UUID_REGEX.match(uuid) or not isinstance(entry, dict):
        return PURGE
    age = now - (_number(entry.get("timestamp")) or now)
    if age >= DISMISSAL_WINDOW_SECONDS or _attempts(entry) >= DISMISSAL_MAX_ATTEMPTS:
        return PURGE
    if cancelled(entry, triggers):
        return CANCEL
    return DUE if next_due(entry, now) <= now + DUE_TOLERANCE_SECONDS else WAIT


def next_due(entry: dict, now: float) -> float:
    """Wall time of the entry's next send (now when never attempted or after a clock step back)."""
    if _attempts(entry) == 0:
        return now
    last = _number(entry.get("last_attempt"))
    if last is None or last > now:
        return now
    return last + DISMISSAL_INTERVAL_SECONDS


def _normalised(entry: dict, now: float) -> dict:
    """Stamp an entry lacking a usable timestamp (or stamped in the future by a clock step) with ``now``."""
    stamp = _number(entry.get("timestamp"))
    return entry if stamp is not None and stamp <= now else {**entry, "timestamp": now}


def plan_dismissals(queue: dict, now: float, triggers: Triggers) -> Tuple[dict, Tuple[str, ...], int]:
    """(queue after purges/cancellations, due uuids, entries dropped) - pure, under the lock."""
    kept: Dict[str, dict] = {}
    due, dropped = [], 0
    for uuid, entry in queue.items():
        if isinstance(entry, dict):
            entry = _normalised(entry, now)
        outcome = verdict(uuid, entry, now, triggers)
        if outcome in (PURGE, CANCEL):
            log_debug(f"Vendor dismissal of {uuid} {'cancelled (vendor fallback/disabled)' if outcome == CANCEL else 'purged'}")
            dropped += 1
            continue
        kept[uuid] = entry
        if outcome == DUE:
            due.append(uuid)
    capped = cap_dismissals(kept)
    return capped, tuple(u for u in due if u in capped), dropped + len(kept) - len(capped)


def earliest_due(queue: object, now: float) -> Optional[float]:
    """When the reconciler must wake for the queue (next send or purge), None for an empty queue."""
    if not isinstance(queue, dict) or not queue:
        return None
    times = []
    for entry in queue.values():
        if not isinstance(entry, dict):
            return now
        stamp = _number(entry.get("timestamp")) or now
        times.append(min(next_due(entry, now), stamp + DISMISSAL_WINDOW_SECONDS))
    return min(times)


def sweep_dismissals(cache_mgr: BoundedSessionCache, herdr_alive: bool,
                     send: Callable[[Sequence[str]], bool]) -> bool:
    """One pass over the queue: purge/cancel under the lock, then ``send`` the due UUIDs outside it.

    ``send`` is the Universal Sender's ``dismiss_vendors`` bound to the reconciler policy
    (POST, then record: purge on 200 or at the 5th attempt). Returns True when any due
    entry is left owed.
    """
    triggers = triggers_for(cache_mgr.state_dir, herdr_alive)
    with cache_mgr as data:
        queue = data.get("dismissed_vendor_uuids") or {}
        updated, due, dropped = plan_dismissals(queue, clock.time(), triggers)
        if updated != queue:
            data["dismissed_vendor_uuids"] = updated
            cache_mgr.save(data)
    if dropped:
        log_debug(f"Vendor dismissal queue: {dropped} entr{'y' if dropped == 1 else 'ies'} purged or cancelled")
    if not due:
        return False
    return send(due)


# -- 12h vendor_active horizon ----------------------------------------------------------
def stale_vendor_candidates(state_dir: Path, now: float) -> Tuple[Path, ...]:
    """``panes/*.vendor_active`` files untouched for more than 12h (listed outside the lock)."""
    panes = Path(state_dir) / "panes"
    if not panes.is_dir():
        return ()
    stale = []
    for path in panes.glob(f"*{VENDOR_ACTIVE_SUFFIX}"):
        try:
            if now - path.stat().st_mtime > VENDOR_ACTIVE_HORIZON_SECONDS:
                stale.append(path)
        except OSError as exc:
            log_debug(f"Could not stat {path.name}: {exc}")
    return tuple(stale)


def _stage_stale(data: dict, paths: Sequence[Path], now: float) -> Tuple[VendorFile, ...]:
    retire = []
    for path in paths:
        record = read_vendor_file(path)
        if record is None:
            continue
        uuids = parse_vendor_uuids(record)
        if not uuids:   # gone, or a bare touch: never swept on mtime
            continue
        for uuid in uuids:
            stage_dismissal_hex(data, uuid, path.name[:-len(VENDOR_ACTIVE_SUFFIX)], now)
        retire.append(record)
    return tuple(retire)


def dismiss_stale_vendor_files(cache_mgr: BoundedSessionCache, herdr_alive: bool, wall_now: float) -> int:
    """Queue the dismissal of each UUID-bearing ``.vendor_active`` older than 12h and retire it.

    Never while Herdr is dead (the vendor fallback must survive), never for a bare touch.
    The queued dismissals are sent by the next ``sweep_dismissals``.
    """
    if not herdr_alive:
        return 0
    candidates = stale_vendor_candidates(cache_mgr.state_dir, wall_now)
    if not candidates:
        return 0
    with cache_mgr as data:
        retire = _stage_stale(data, candidates, clock.time())
        if retire:
            cache_mgr.save(data)
    for record in retire:   # only after the queued dismissals are saved
        retire_vendor_file(record)
    if retire:
        log_debug(f"Queued dismissal of {len(retire)} vendor session(s) idle for more than 12h")
    return len(retire)
