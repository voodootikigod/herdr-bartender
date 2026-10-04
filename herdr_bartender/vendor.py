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
   (R11) or records the attempt for the reconciler's 2s retry cadence.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple

from . import clock, runtime
from .bridge import DEFAULT_EVENT_TIMEOUT, DeliveryResult, send_event
from .cache import BoundedSessionCache, CacheError, IntegrationDisabled
from .config import VENDOR_UUID_REGEX
from .delivery_state import clear_pending_vendor_cleanup
from .log import log_debug, log_warning
from .paths import get_state_dir
from .sanitize import get_hex_pane_id

VENDOR_ACTIVE_SUFFIX = ".vendor_active"
CLAIM_INFIX = ".claim-"              # <hex>.vendor_active.claim-<pid>-<ns>: being retired (leftovers swept)
DISMISSAL_AGENT = "Herdr"            # R19
NETWORK_RESERVE_SECONDS = 0.3


@dataclass(frozen=True)
class VendorFile:
    """A ``.vendor_active`` file as read under the cache lock (``content`` None: unreadable)."""

    path: Path
    content: Optional[bytes]


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
    """The file and the bytes read now; None when it does not exist."""
    try:
        return VendorFile(path, path.read_bytes())
    except FileNotFoundError:
        return None
    except OSError as exc:
        log_debug(f"Unreadable {path.name} ({exc}); treating it as a bare touch")
        return VendorFile(path, None)


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
        parsed = json.loads(text)
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

    The guard rewrites ``.vendor_active`` without the cache lock (mktemp + ``mv -f``), so
    the file is first claimed by an atomic rename; claimed bytes that differ from what
    was read (and queued) belong to a newer record, which is put back.
    """
    claimed = _claimed_path(record.path)
    try:
        os.rename(record.path, claimed)
    except FileNotFoundError:
        return
    except OSError as exc:
        log_warning(f"Could not claim {record.path.name} for removal: {exc}")
        return
    if record.content is not None:
        current = read_vendor_file(claimed)
        if current is None or current.content != record.content:
            _put_back(claimed, record.path)
    _discard(claimed)


def pending_vendor_panes(data: dict) -> Tuple[str, ...]:
    """Panes with a persisted ``pending_vendor_cleanups`` entry (Plan §4.3 L486), oldest first."""
    entries = data.get("pending_vendor_cleanups", [])
    panes = (entry.get("pane_id") for entry in entries if isinstance(entry, dict))
    return tuple(dict.fromkeys(pane for pane in panes if isinstance(pane, str) and pane))


def stage_dismissal(data: dict, uuid: str, pane_id: str, now: float) -> dict:
    """Queue ``uuid`` in ``dismissed_vendor_uuids`` before anything is sent (attempts 0)."""
    entry = {"timestamp": now, "pane_hex": get_hex_pane_id(pane_id), "attempts": 0, "last_attempt": 0.0}
    data.setdefault("dismissed_vendor_uuids", {})[uuid] = entry
    return entry


def resolve_vendor_cleanups(data: dict, panes: Sequence[str], now: float) -> VendorResolution:
    """Under the cache lock: queue each pane's vendor dismissal and clear its pending cleanup entry."""
    dismissals, unlink, resolved = [], [], []
    for pane in dict.fromkeys(p for p in panes if p):
        if pane in pending_vendor_panes(data):
            clear_pending_vendor_cleanup(data, pane)
            resolved.append(pane)
        record = read_vendor_file(vendor_active_path(pane))
        if record is None:
            continue
        uuid = parse_vendor_uuid(record)
        if uuid:
            stage_dismissal(data, uuid, pane, now)
            dismissals.append(uuid)
        unlink.append(record)
    return VendorResolution(tuple(dict.fromkeys(dismissals)), tuple(unlink), tuple(resolved))


def dismissal_payload(uuid: str) -> dict:
    return {"state": "Ended", "agent": DISMISSAL_AGENT, "session_id": uuid}


def post_dismissal(uuid: str, timeout: float = DEFAULT_EVENT_TIMEOUT,
                   bridge_url: Optional[str] = None) -> DeliveryResult:
    """Send the vendor Ended (outside the lock; the caller checked the budget)."""
    return send_event(dismissal_payload(uuid), timeout=timeout, bridge_url=bridge_url)


def record_dismissal_attempt(data: dict, uuid: str, result: DeliveryResult, now: float) -> None:
    """Under the lock: purge on HTTP 200 (R11), else count the attempt for the reconciler's retries."""
    queue = data.setdefault("dismissed_vendor_uuids", {})
    entry = queue.get(uuid)
    if not isinstance(entry, dict):
        return
    if result.success:
        queue.pop(uuid, None)
        return
    queue[uuid] = {**entry, "attempts": int(entry.get("attempts", 0) or 0) + 1, "last_attempt": now}


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

    ``raw_pane_id`` and ``is_pane_closed`` are accepted for compatibility: a bare touch is unlinked on
    confirmed delivery and on pane close alike (Plan §1 L57).
    """
    if not pane_id:
        return
    cache_mgr = BoundedSessionCache(get_state_dir(), check_disabled=True)
    try:
        with cache_mgr as data:
            resolution = resolve_vendor_cleanups(data, (pane_id,), clock.time())
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
