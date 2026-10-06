"""Spool envelope schema (R6), close classification and the shared atomic-file helpers.

Used by the spool (contention envelopes), the results directory and corrupt-cache
salvage. Every write here is tmp + fsync + ``os.replace``; every quarantine keeps
its ``bad/`` directory bounded after each move (gap spool-close-protection-agent-exit).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional

from . import jsonsafe
from .intake import EVENT_NAMES, PANE_CLOSED, TAB_CLOSED, WORKSPACE_CLOSED, is_agent_exit_signal
from .log import log_debug
from .boundedio import capped_directory, open_exclusive_tmp, read_regular_file
from .paths import PRIVATE_FILE_MODE, ensure_private_dir

MAX_ENVELOPE_BYTES = 256 * 1024   # R62/R64: spool and results envelopes are refused / unread past this

CLOSE_EVENTS = frozenset({PANE_CLOSED, TAB_CLOSED, WORKSPACE_CLOSED})
BAD_DIR_CAP = 20          # Plan §4.3 L396: spool/bad holds at most 20 files
ENVELOPE_KEYS = ("event_name", "event_data", "context", "arrival_ns", "enqueued_ns")


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def build_envelope(event_name: str, event_data: dict, context: dict,
                   arrival_ns: int, enqueued_ns: int) -> dict:
    """R6 contention envelope. ``generation`` is deliberately omitted (Plan §4.3 L391)."""
    return {
        "event_name": event_name,
        "event_data": dict(event_data) if isinstance(event_data, dict) else {},
        "context": dict(context) if isinstance(context, dict) else {},
        "arrival_ns": int(arrival_ns),
        "enqueued_ns": int(enqueued_ns),
    }


def validate_envelope(env: object) -> Optional[str]:
    """None when ``env`` is a replayable R6 envelope, else the reason it is poison."""
    if not isinstance(env, dict):
        return "envelope is not a JSON object"
    if env.get("event_name") not in EVENT_NAMES:
        return f"unknown event_name {str(env.get('event_name'))[:64]!r}"
    if not isinstance(env.get("event_data"), dict):
        return "event_data is not an object"
    if not isinstance(env.get("context", {}), dict):
        return "context is not an object"
    if not _positive_int(env.get("arrival_ns")) or not _positive_int(env.get("enqueued_ns")):
        return "arrival_ns/enqueued_ns must be positive integers"
    generation = env.get("generation")
    if generation is not None and not _positive_int(generation):
        return "generation must be a positive integer when present"
    return None


def is_close_envelope(env: object) -> bool:
    """Container closes, explicit ``state == Ended`` and agent exits (Plan §4.3 L397, §6.3 step 2)."""
    if not isinstance(env, dict):
        return False
    if env.get("event_name") in CLOSE_EVENTS:
        return True
    data = env.get("event_data")
    if not isinstance(data, dict):
        return False
    return data.get("state") == "Ended" or is_agent_exit_signal(data)


def read_json(path: Path, max_bytes: int = MAX_ENVELOPE_BYTES) -> object:
    """Decode a bounded regular JSON file (R64); raises OSError or ValueError (incl. OversizedFile)."""
    return jsonsafe.loads(read_regular_file(path, max_bytes))


def write_json_capped(directory: Path, path: Path, obj: object, cap: int, lock_timeout: float,
                      max_bytes: int = MAX_ENVELOPE_BYTES) -> None:
    """R62/R64: ``write_json_atomic`` under the directory lock, refused at ``cap`` files or past ``max_bytes``.

    Raises DirectoryFull (OSError) at the ceiling or when the lock is busy, ValueError when too large.
    """
    data = serialize_json(obj)   # R65: the size checked is exactly the size written
    if len(data) > max_bytes:
        raise ValueError(f"envelope too large ({len(data)} bytes > {max_bytes})")
    with capped_directory(directory, cap, lock_timeout):
        write_bytes_atomic(path, data)


def serialize_json(obj: object) -> bytes:
    """The exact bytes every envelope writer persists (compact JSON, UTF-8)."""
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def write_json_atomic(path: Path, obj: object) -> None:
    """Write ``obj`` to ``path`` via ``<path>.tmp`` + fsync + replace (0600).

    Raises OSError/TypeError/ValueError on failure, after unlinking the tmp file.
    """
    write_bytes_atomic(path, serialize_json(obj))


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via ``<path>.tmp`` + fsync + replace (0600); unlinks the tmp file on failure."""
    tmp = path.with_name(f"{path.name}.tmp")
    fd = open_exclusive_tmp(tmp)
    try:
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        _unlink_quietly(tmp)
        raise


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log_debug(f"Could not remove {path}: {e}")


def prune_dir(directory: Path, cap: int) -> List[Path]:
    """Unlink the oldest ``*.json`` files beyond ``cap`` (by mtime, then name); returns them."""
    try:
        files = [p for p in directory.glob("*.json") if p.is_file()]
        files.sort(key=lambda p: (p.stat().st_mtime, p.name))
    except OSError as e:
        log_debug(f"Could not list {directory} for pruning: {e}")
        return []
    excess = files[:max(0, len(files) - cap)]
    for path in excess:
        _unlink_quietly(path)
    return excess


def quarantine(path: Path, bad_dir: Path, reason: str, cap: int = BAD_DIR_CAP) -> bool:
    """Move a poison file into ``bad_dir`` (0700) and re-apply the cap at once; False if it could not move."""
    try:
        ensure_private_dir(bad_dir)
        os.replace(path, bad_dir / path.name)
    except OSError as e:
        log_debug(f"Could not quarantine {path.name} ({reason}): {e}; unlinking it")
        _unlink_quietly(path)
        return False
    log_debug(f"Quarantined {path.name} to {bad_dir.name}/: {reason}")
    prune_dir(bad_dir, cap)
    return True


def unlink_files(paths: List[Path]) -> None:
    for path in paths:
        _unlink_quietly(path)
