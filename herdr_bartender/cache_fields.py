"""Field-level sanity of a decoded cache (Plan §6.3 salvage complement; R55).

``normalize_cache`` fixes the root shape; this fixes the known fields inside it. A known numeric field must hold a
finite number (a numeric string is converted), a known text field a string, a known object field an object and a
safety flag a real boolean; ``None`` is always accepted. Entries of the pending cleanup/compensation lists must be
objects naming their pane / session. Anything else is dropped, so its default applies, and logged. Tombstone and agent-exit
entries must be objects whose times are numbers; others are dropped (they only live 60s). Unknown fields are left
alone. This runs on every load (in place), before any code calls ``int()``/``float()`` on these fields.
"""

from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional, Tuple

from .log import log_warning

INT_FIELDS = (
    "seq", "delivered_seq", "rejected_seq", "transmitting_seq", "generation", "resync_generation",
    "lease_resync_gen", "delivery_attempts", "sending_pid", "admitted_at_ns", "last_arrival_ns", "last_event_ns",
    "closed_at_ns", "exit_at_ns",
)
NUMBER_FIELDS = (
    "last_event_at", "lease_deadline", "next_retry_at", "orphaned_at", "ttl_expired_at", "last_source_timestamp",
    "closed_source_ts", "exit_source_ts", "last_applied_arrival_time",
)
TEXT_FIELDS = (
    "pane_id", "workspace_id", "tab_id", "host", "agent", "raw_agent", "title", "cwd", "desired_state",
    "delivered_state", "delivery_status", "delivery_error", "lease_token", "close_kind", "expiry_reason",
)
OBJECT_FIELDS = ("desired_payload", "admission_signal", "positive_signal")
# Safety flags decide eviction: a truthy non-boolean ("false") must never pass for True.
FLAG_FIELDS = ("salvaged", "orphaned_ended", "orphan_mirror_owed")
ROOT_INT_DEFAULTS = {"consecutive_failures": 0}
ROOT_NUMBER_FIELDS = ("last_updated", "last_successful_delivery", "herdr_dead_since")
TOMBSTONE_TIMES = ("closed_at_ns", "closed_source_ts", "last_source_timestamp")
AGENT_EXIT_TIMES = ("exit_at_ns", "exit_source_ts")

_DROP = object()


def _finite(value: float) -> object:
    return value if math.isfinite(value) else _DROP


def _as_number(value: object) -> object:
    """A finite int/float, a numeric string converted, else _DROP."""
    if isinstance(value, bool):
        return _DROP
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return _finite(value)
    if isinstance(value, str):
        try:
            return _finite(float(value))
        except (ValueError, OverflowError):
            return _DROP
    return _DROP


def _as_int(value: object) -> object:
    number = _as_number(value)
    if number is _DROP or isinstance(number, int):
        return number
    try:
        return int(number)   # a finite float: int() never fails
    except (ValueError, OverflowError):
        return _DROP


def _check(value: object, kind: str) -> object:
    if value is None:
        return None
    if kind == "int":
        return _as_int(value)
    if kind == "number":
        return _as_number(value)
    if kind == "text":
        return value if isinstance(value, str) else _DROP
    if kind == "flag":
        return value if isinstance(value, bool) else _DROP
    return value if isinstance(value, dict) else _DROP


_SESSION_KINDS: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (INT_FIELDS, "int"), (NUMBER_FIELDS, "number"), (TEXT_FIELDS, "text"), (OBJECT_FIELDS, "object"),
    (FLAG_FIELDS, "flag"),
)
# Root lists: (field, the key every entry must carry as a non-empty string). Other entries are dropped.
LIST_ENTRY_KEYS = (("pending_vendor_cleanups", "pane_id"), ("pending_compensations", "session_id"))


def sane_record(record: dict) -> List[str]:
    """Fix ``record`` in place; returns the names of the dropped fields."""
    dropped = []
    for fields, kind in _SESSION_KINDS:
        for name in fields:
            if name not in record:
                continue
            value = _check(record[name], kind)
            if value is _DROP:
                del record[name]
                dropped.append(name)
            elif value is not record[name]:
                record[name] = value
    return dropped


def _sane_entries(entries: Dict[str, object], times: Tuple[str, ...]) -> Dict[str, object]:
    """Entries that are objects whose present time fields are numbers (converted like session fields)."""
    kept = {}
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        checked = {name: _as_number(entry[name]) for name in times if name in entry}
        if any(value is _DROP for value in checked.values()):
            continue
        kept[key] = {**entry, **checked}
    return kept


def _sane_root(data: dict) -> List[str]:
    dropped = []
    for name, default in ROOT_INT_DEFAULTS.items():
        value = _check(data.get(name, default), "int")
        if value is _DROP:
            dropped.append(name)
        data[name] = default if value is _DROP or value is None else value
    for name in ROOT_NUMBER_FIELDS:
        if name in data and _check(data[name], "number") is _DROP:
            data[name] = None
            dropped.append(name)
    return dropped


def sane_cache_fields(data: dict, warn: Optional[Callable[[str], None]] = None) -> dict:
    """Fix the known fields of a normalized cache ``data`` in place (and return it)."""
    warn = warn or log_warning
    notes = [f"root {name}" for name in _sane_root(data)]
    for sid, record in data.get("sessions", {}).items():
        notes.extend(f"{sid} {name}" for name in sane_record(record))
    for name, key in LIST_ENTRY_KEYS:
        before = data.get(name, [])
        data[name] = [e for e in before if isinstance(e, dict) and isinstance(e.get(key), str) and e[key]]
        dropped = len(before) - len(data[name])
        if dropped:
            notes.append(f"{name}: {dropped} malformed entries")
    for name, times in (("tombstones", TOMBSTONE_TIMES), ("agent_exits", AGENT_EXIT_TIMES)):
        before = data.get(name, {})
        data[name] = _sane_entries(before, times)
        notes.extend(f"{name} {key}" for key in before if key not in data[name])
    if notes:
        warn(f"Cache fields of the wrong type dropped (defaults apply): {', '.join(notes[:20])}"
             + (f" and {len(notes) - 20} more" if len(notes) > 20 else ""))
    return data
