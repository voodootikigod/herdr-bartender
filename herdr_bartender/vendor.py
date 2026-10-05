"""Vendor (``.vendor_active``) cleanup and dismissal (Plan §1 L9/L38/L56-57, §3.2, §5.1 item 5b, R11, R19).

A dismissal is always STAGED before it is SENT (critic vendor-dismissal-send-before-stage):

1. Under the cache lock, ``resolve_vendor_cleanups()`` reads each pane's
   ``.vendor_active``. A UUID record is queued in ``dismissed_vendor_uuids``
   (``attempts: 0, last_attempt: 0``) and the pane's ``pending_vendor_cleanups``
   entry is cleared; a bare touch only needs unlinking. The returned
   ``VendorResolution.commit()`` retires the files AFTER the cache was saved, so a
   dismissal is never lost: if the process dies anywhere later, the reconciler owns
   the queued entry. The guard rewrites ``.vendor_active`` without the cache lock, so
   a file is retired only if it still holds the bytes that were read and queued
   (claimed by an atomic rename first; a newer record is put back, never deleted).
2. Outside the lock (budget-gated by the caller), ``post_dismissal()`` sends
   ``{"state": "Ended", "agent": "Herdr", "session_id": <uuid>}`` (R19).
3. Under the lock again, ``record_dismissal_attempt()`` purges the entry on HTTP 200
   or after its 5th attempt (R11), else records the attempt for the reconciler's 2s
   retry cadence (``dismissals.sweep_dismissals``).

A dismissal queued by a pane/tab/workspace close carries ``pane_closed: true``: the pane
and the vendor CLI that ran in it are gone, so the reconciler's pane-scoped cancellation
triggers (``.vendor_active``, ``.failed``) do not apply to it (R24, see ``dismissals``).
Other confirmed Endeds (TTL, agent exit, Herdr dead/restart, --cleanup) do not set it.
The queue holds at most ``DISMISSED_VENDOR_CAP`` (64) entries; staging a new one
prunes the oldest.

R35: while Herdr is dead or the integration is off (``DISABLED``/``NO_HOOKS``) the vendor
fallback is the live representation, so step 1 cancels the cleanup instead (the pending
entry is dropped; ``.vendor_active`` is neither read nor unlinked; nothing is sent).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple

from . import clock, jsonsafe, runtime
from .bridge import DEFAULT_EVENT_TIMEOUT, DeliveryResult, send_event
from .cache import BoundedSessionCache, CacheError, IntegrationDisabled
from .cache_schema import DISMISSED_VENDOR_CAP
from .config import VENDOR_UUID_REGEX
from .delivery_state import clear_pending_vendor_cleanup
from .log import log_debug, log_warning
from .paths import get_state_dir
from .process import is_herdr_alive, memoised_herdr_alive
from .sanitize import get_hex_pane_id

VENDOR_ACTIVE_SUFFIX = ".vendor_active"
VENDOR_FILE_MAX_BYTES = 4096   # R63: a real record is ~100 bytes; larger is unusable and never read whole
CLAIM_INFIX = ".claim-"              # <hex>.vendor_active.claim-<pid>-<ns>: being retired (leftovers swept)
DISMISSAL_AGENT = "Herdr"            # R19
DISMISSAL_MAX_ATTEMPTS = 5           # R11: sends at t=0, 2, 4, 6, 8
NETWORK_RESERVE_SECONDS = 0.3


@dataclass(frozen=True)
class VendorFile:
    """A ``.vendor_active`` file as read under the cache lock (``content`` None: unreadable).

    ``identity`` is ``(st_ino, st_mtime_ns)`` of the bytes read: the guard refreshes an unchanged
    record (mktemp + ``mv -f``, then ``touch``), which only the identity shows.
    """

    path: Path
    content: Optional[bytes]
    identity: Optional[Tuple[int, int]] = None


@dataclass(frozen=True)
class VendorResolution:
    """Dismissals queued under the lock, and the ``.vendor_active`` files to retire once the cache is saved."""

    dismissals: Tuple[str, ...] = ()
    unlink: Tuple[VendorFile, ...] = ()
    resolved_panes: Tuple[str, ...] = ()   # panes whose pending_vendor_cleanups entry was cleared

    @property
    def changed(self) -> bool:
        """The cache was modified (a dismissal queued or a pending entry cleared): it must be saved."""
        return bool(self.dismissals or self.resolved_panes)

    def commit(self) -> None:
        """Retire the resolved ``.vendor_active`` files. Call only after the cache save succeeded."""
        for record in self.unlink:
            retire_vendor_file(record)


def vendor_active_path(pane_id: str) -> Path:
    return get_state_dir() / "panes" / f"{get_hex_pane_id(pane_id)}{VENDOR_ACTIVE_SUFFIX}"


def read_vendor_file(path: Path) -> Optional[VendorFile]:
    """The file, the bytes read now and their identity; None when it does not exist."""
    try:
        with open(path, "rb") as handle:
            stat = os.fstat(handle.fileno())
            content = handle.read(VENDOR_FILE_MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        log_debug(f"Unreadable {path.name} ({exc}); treating it as a bare touch, but never retiring it")
        return VendorFile(path, None)
    if len(content) > VENDOR_FILE_MAX_BYTES:
        log_warning(f"Oversized {path.name} (> {VENDOR_FILE_MAX_BYTES} bytes); treating it as unreadable")
        return VendorFile(path, None)
    return VendorFile(path, content, (stat.st_ino, stat.st_mtime_ns))


def parse_vendor_uuid(record: VendorFile) -> Optional[str]:
    """The valid vendor UUID in ``record``, or None for a bare touch / unusable record."""
    try:
        text = (record.content or b"").decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        log_debug(f"Undecodable {record.path.name} ({exc}); treating it as a bare touch")
        return None
    if not text.startswith("{"):
        return None
    try:
        parsed = jsonsafe.loads(text)
    except ValueError:
        return None
    uuid = parsed.get("vendor_session_id") if isinstance(parsed, dict) else None
    if isinstance(uuid, str) and VENDOR_UUID_REGEX.match(uuid):
        return uuid
    log_debug(f"Ignoring malformed vendor_session_id in {record.path.name}; treating it as a bare touch")
    return None


def _claimed_path(path: Path) -> Path:
    return path.with_name(f"{path.name}{CLAIM_INFIX}{os.getpid()}-{clock.time_ns()}")


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log_warning(f"Could not unlink {path.name}: {exc}")


def _put_back(claimed: Path, path: Path) -> None:
    """Restore a record the guard wrote after it was read, never over an even newer one."""
    try:
        os.link(claimed, path)
        log_debug(f"{path.name} was rewritten after it was read; the newer record is kept")
        return
    except FileExistsError:
        log_warning(f"{path.name} was rewritten twice while being retired; the newest record is kept")
        return
    except OSError as exc:  # no hard links on this filesystem: rename it back unless a newer one appeared
        log_debug(f"Could not link {path.name} back ({exc}); renaming it back")
    try:
        if not path.exists():
            os.rename(claimed, path)
    except OSError as exc:
        log_warning(f"Could not restore the rewritten {path.name}: {exc}")


def retire_vendor_file(record: VendorFile) -> None:
    """Unlink the record read under the lock (outside it, after the save), never a newer one.

    A record that could not be read is left in place (its bytes and identity were never observed). The guard
    rewrites ``.vendor_active`` without the cache lock (mktemp + ``mv -f``), so the file is first claimed by an
    atomic rename; a claimed file whose bytes OR identity
    (inode, mtime) differ from what was read (and queued) is a newer record - possibly the
    same UUID refreshed by a pass-through, i.e. a live vendor fallback - and is put back.
    """
    if record.content is None:   # never observed: whatever is there now may be a newer record (gate, round 11)
        log_warning(f"{record.path.name} could not be read; it is left in place, not retired")
        return
    claimed = _claimed_path(record.path)
    try:
        os.rename(record.path, claimed)
    except FileNotFoundError:
        return
    except OSError as exc:
        log_warning(f"Could not claim {record.path.name} for removal: {exc}")
        return
    if _rewritten_since(record, read_vendor_file(claimed)):
        _put_back(claimed, record.path)
    _discard(claimed)


def _rewritten_since(record: VendorFile, current: Optional[VendorFile]) -> bool:
    """The claimed file is not the one ``record`` read (a rename keeps the inode and mtime)."""
    if current is None or current.content != record.content:
        return True
    return record.identity is not None and current.identity != record.identity


def pending_vendor_panes(data: dict) -> Tuple[str, ...]:
    """Panes with a persisted ``pending_vendor_cleanups`` entry (Plan §4.3 L486), oldest first."""
    entries = data.get("pending_vendor_cleanups", [])
    panes = (entry.get("pane_id") for entry in entries if isinstance(entry, dict))
    return tuple(dict.fromkeys(pane for pane in panes if isinstance(pane, str) and pane))


def _stamp(entry: object) -> float:
    value = entry.get("timestamp") if isinstance(entry, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def cap_dismissals(queue: dict, keep: Optional[str] = None) -> dict:
    """At most DISMISSED_VENDOR_CAP entries, the oldest pruned first (``keep`` is never pruned)."""
    if len(queue) <= DISMISSED_VENDOR_CAP:
        return queue
    candidates = sorted((uuid for uuid in queue if uuid != keep), key=lambda uuid: _stamp(queue[uuid]))
    dropped = set(candidates[:len(queue) - DISMISSED_VENDOR_CAP])
    log_debug(f"dismissed_vendor_uuids over its {DISMISSED_VENDOR_CAP}-entry cap; pruning {len(dropped)} oldest")
    return {uuid: entry for uuid, entry in queue.items() if uuid not in dropped}


def stage_dismissal_hex(data: dict, uuid: str, pane_hex: str, now: float, pane_closed: bool = False) -> dict:
    """Queue ``uuid`` in ``dismissed_vendor_uuids`` before anything is sent (attempts 0), within the 64 cap."""
    entry = {"timestamp": now, "pane_hex": pane_hex, "attempts": 0, "last_attempt": 0.0,
             "pane_closed": bool(pane_closed)}
    queue = dict(data.get("dismissed_vendor_uuids") or {})
    queue[uuid] = entry
    data["dismissed_vendor_uuids"] = cap_dismissals(queue, keep=uuid)
    return entry


def stage_dismissal(data: dict, uuid: str, pane_id: str, now: float, pane_closed: bool = False) -> dict:
    return stage_dismissal_hex(data, uuid, get_hex_pane_id(pane_id), now, pane_closed)


def _closed_panes(data: dict) -> frozenset:
    """Panes whose persisted ``pending_vendor_cleanups`` entry comes from a pane close."""
    entries = data.get("pending_vendor_cleanups", [])
    return frozenset(e.get("pane_id") for e in entries if isinstance(e, dict) and e.get("is_pane_closed"))


def fallback_protected(state_dir: Optional[Path] = None) -> bool:
    """R35: the vendor fallback is the live representation, so no cleanup may dismiss or unlink it.

    True while the integration is off (``DISABLED``, ``NO_HOOKS``: vendor hooks run unguarded) or Herdr is
    confirmed dead (Plan §3.2 Codex row item 3: ``.vendor_active`` is kept until Herdr recovers, the pane
    closes or a vendor session-terminal event unlinks it). Safe under the cache lock: Herdr liveness comes
    from the 0.5s memo only, which callers that may run while Herdr is dead warm before locking.
    """
    directory = Path(state_dir) if state_dir is not None else get_state_dir()
    if (directory / "DISABLED").exists() or (directory / "NO_HOOKS").exists():
        return True
    return not memoised_herdr_alive()


def warm_fallback_probe() -> None:
    """Resolve Herdr liveness before the cache lock (memoised 0.5s) for ``fallback_protected`` under it.

    For callers that may run while Herdr is dead (the reconciler, --cleanup); the event path runs because
    Herdr invoked it, so it relies on whatever is memoised (not memoised counts as alive).
    """
    is_herdr_alive()


def _cancel_cleanups(data: dict, panes: Sequence[str]) -> VendorResolution:
    """R35: drop the panes' pending cleanups without touching ``.vendor_active`` or queueing a dismissal."""
    resolved = [pane for pane in dict.fromkeys(p for p in panes if p) if pane in pending_vendor_panes(data)]
    for pane in resolved:
        clear_pending_vendor_cleanup(data, pane)
    if panes:
        log_debug(f"Vendor fallback protected (Herdr dead / integration off): cleanup of {list(panes)} cancelled")
    return VendorResolution(resolved_panes=tuple(resolved))


def resolve_vendor_cleanups(data: dict, panes: Sequence[str], now: float,
                            closed_panes: Sequence[str] = ()) -> VendorResolution:
    """Under the cache lock: queue each pane's vendor dismissal and clear its pending cleanup entry.

    A pane closed by this request (``closed_panes``, or a persisted ``is_pane_closed`` entry) marks its
    queued dismissal ``pane_closed``. While ``fallback_protected()`` the cleanups are cancelled instead (R35).
    """
    if panes and fallback_protected():
        return _cancel_cleanups(data, panes)
    dismissals, unlink, resolved = [], [], []
    closed = _closed_panes(data) | frozenset(closed_panes)
    for pane in dict.fromkeys(p for p in panes if p):
        if pane in pending_vendor_panes(data):
            clear_pending_vendor_cleanup(data, pane)
            resolved.append(pane)
        record = read_vendor_file(vendor_active_path(pane))
        if record is None:
            continue
        uuid = parse_vendor_uuid(record)
        if uuid:
            stage_dismissal(data, uuid, pane, now, pane_closed=pane in closed)
            dismissals.append(uuid)
        unlink.append(record)
    return VendorResolution(tuple(dict.fromkeys(dismissals)), tuple(unlink), tuple(resolved))


def dismissal_payload(uuid: str) -> dict:
    return {"state": "Ended", "agent": DISMISSAL_AGENT, "session_id": uuid}


def post_dismissal(uuid: str, timeout: float = DEFAULT_EVENT_TIMEOUT,
                   bridge_url: Optional[str] = None) -> DeliveryResult:
    """Send the vendor Ended (outside the lock; the caller checked the budget)."""
    return send_event(dismissal_payload(uuid), timeout=timeout, bridge_url=bridge_url)


def _attempts(entry: dict) -> int:
    value = entry.get("attempts", 0)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def record_dismissal_attempt(data: dict, uuid: str, result: DeliveryResult, now: float) -> None:
    """Under the lock: purge on HTTP 200 or at the 5th attempt (R11), else count the attempt for the retries."""
    queue = data.setdefault("dismissed_vendor_uuids", {})
    entry = queue.get(uuid)
    if not isinstance(entry, dict):
        return
    attempts = _attempts(entry) + 1
    if result.success or attempts >= DISMISSAL_MAX_ATTEMPTS:
        if not result.success:
            log_debug(f"Vendor dismissal of {uuid} unconfirmed after {attempts} attempts; purged (R11)")
        queue.pop(uuid, None)
        return
    queue[uuid] = {**entry, "attempts": attempts, "last_attempt": now}


def record_dismissal_attempts(cache_mgr: BoundedSessionCache, sent: Sequence[Tuple[str, DeliveryResult]]) -> bool:
    """Persist the outcome of each sent dismissal; False when the cache could not be updated."""
    if not sent:
        return True
    try:
        with cache_mgr as data:
            now = clock.time()
            for uuid, result in sent:
                record_dismissal_attempt(data, uuid, result, now)
            cache_mgr.save(data)
    except (CacheError, IntegrationDisabled) as exc:
        log_debug(f"Vendor dismissal bookkeeping left to the reconciler: {exc!r}")
        return False
    return True


def cleanup_vendor_active(pane_id: str, raw_pane_id: Optional[str] = None, is_pane_closed: bool = False,
                          bridge_url: Optional[str] = None) -> None:
    """Stage, send and record one pane's vendor dismissal with its own lock holds (reconciler / --cleanup).

    ``raw_pane_id`` is accepted for compatibility. A bare touch is unlinked on confirmed delivery and on
    pane close alike (Plan §1 L57); ``is_pane_closed`` marks the queued dismissal ``pane_closed``.
    """
    if not pane_id:
        return
    cache_mgr = BoundedSessionCache(get_state_dir(), check_disabled=True)
    warm_fallback_probe()
    try:
        with cache_mgr as data:
            resolution = resolve_vendor_cleanups(data, (pane_id,), clock.time(),
                                                 closed_panes=(pane_id,) if is_pane_closed else ())
            cache_mgr.save(data)
            resolution.commit()
    except IntegrationDisabled:
        log_debug(f"DISABLED: vendor cleanup for {pane_id} skipped")
        return
    except CacheError as exc:
        log_debug(f"Vendor cleanup for {pane_id} not staged ({exc}); .vendor_active kept")
        return
    sent = []
    for uuid in resolution.dismissals:
        if not runtime.budget_allows(NETWORK_RESERVE_SECONDS):
            log_debug(f"Vendor dismissal of {uuid} left queued for the reconciler (budget)")
            continue
        sent.append((uuid, post_dismissal(uuid, bridge_url=bridge_url)))
    record_dismissal_attempts(cache_mgr, sent)
