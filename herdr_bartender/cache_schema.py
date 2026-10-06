"""active-sessions.json schema v4 (Plan §4.2, §4.3): fresh caches and migration of older ones.

Pure functions: nothing here touches the filesystem or the lock (``cache_fields`` may log a dropped field).
"""

from __future__ import annotations

import math
from typing import Callable, Dict, Iterable

from .cache_fields import sane_cache_fields

SCHEMA_VERSION = 4
SALVAGE_EPOCH_FLOOR = 1_700_000_000   # Plan §6.3 step 4: epoch-dominating salvage generation floor
DISMISSED_VENDOR_CAP = 64             # Plan §1 L56

DICT_FIELDS = ("sessions", "pane_generations", "tombstones", "agent_exits", "dismissed_vendor_uuids")
LIST_FIELDS = ("pending_compensations", "pending_vendor_cleanups")
SCALAR_DEFAULTS = {
    "cache_seq": 0,
    "herdr_instance_id": None,
    "last_herdr_pid": None,
    "last_bartender_pid": None,
    "herdr_dead_since": None,     # Plan §5.1 item 7: wall time Herdr was first confirmed dead (reconciler)
    "last_updated": 0.0,
    "consecutive_failures": 0,
    "last_successful_delivery": 0.0,
    "next_generation": 1,
}


class CorruptCache(ValueError):
    """The decoded JSON is not a usable cache (wrong root type or no ``sessions`` mapping)."""


def new_cache(host: str) -> dict:
    """A fresh v4 cache with ``host`` pinned (Plan §4.1 L273)."""
    data: dict = {"version": SCHEMA_VERSION, "host": host}
    data.update(SCALAR_DEFAULTS)
    data.update({name: {} for name in DICT_FIELDS})
    data.update({name: [] for name in LIST_FIELDS})
    return data


def _int_or(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _valid_sessions(raw: dict) -> Dict[str, dict]:
    return {sid: rec for sid, rec in raw.items() if isinstance(sid, str) and isinstance(rec, dict)}


def _valid_generations(raw: dict) -> Dict[str, int]:
    return {
        pane: value for pane, value in raw.items()
        if isinstance(pane, str) and isinstance(value, int) and not isinstance(value, bool)
    }


def _dominating_generation(data: dict) -> int:
    """max(next_generation, every pane generation, every session generation, 1) (Plan §4.1 L262-265)."""
    candidates: Iterable[int] = [
        _int_or(data.get("next_generation"), 1),
        *data["pane_generations"].values(),
        *(_int_or(rec.get("generation"), 0) for rec in data["sessions"].values()),
        1,
    ]
    return max(candidates)


def _cap_dismissed(dismissed: dict) -> dict:
    if len(dismissed) <= DISMISSED_VENDOR_CAP:
        return dismissed

    def stamp(item) -> float:
        """Total sort key: an unusable stamp (missing, non-numeric, NaN) sorts oldest, never raises."""
        value = item[1].get("timestamp") if isinstance(item[1], dict) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return float("-inf")
        number = float(value)
        return number if math.isfinite(number) else float("-inf")

    newest = sorted(dismissed.items(), key=stamp)[-DISMISSED_VENDOR_CAP:]
    return dict(newest)


def normalize_cache(raw: object, current_host: Callable[[], str]) -> dict:
    """Return a v4 copy of a decoded cache; raises CorruptCache when it is structurally unusable.

    Missing or wrong-typed root fields get their defaults, non-dict session records
    are dropped, the pinned host is kept (or pinned now when absent) and
    next_generation is raised to dominate every stored generation.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("sessions"), dict):
        raise CorruptCache("cache root is not an object with a 'sessions' mapping")
    data = dict(raw)
    for name in DICT_FIELDS:
        if not isinstance(data.get(name), dict):
            data[name] = {}
    for name in LIST_FIELDS:
        if not isinstance(data.get(name), list):
            data[name] = []
    for name, default in SCALAR_DEFAULTS.items():
        data.setdefault(name, default)
    data["sessions"] = _valid_sessions(data["sessions"])
    data["pane_generations"] = _valid_generations(data["pane_generations"])
    data["dismissed_vendor_uuids"] = _cap_dismissed(data["dismissed_vendor_uuids"])
    data["cache_seq"] = _int_or(data.get("cache_seq"), 0)
    data["next_generation"] = _dominating_generation(data)
    if not isinstance(data.get("host"), str) or not data["host"]:
        data["host"] = current_host()
    data["version"] = SCHEMA_VERSION
    return sane_cache_fields(data)   # R55: wrong-typed known fields are dropped before any code coerces them
