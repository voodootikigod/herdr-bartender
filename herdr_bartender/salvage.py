"""Corrupt-cache quarantine and quiescent salvage (Plan §6.3).

``build_salvaged_cache`` is pure (raw text in, fresh v4 cache out). The file
operations (quarantine copy, 7-day prune, selective spool quarantine, per-pane
marker removal) are separate helpers that ``cache.BoundedSessionCache`` runs while
it holds the cache lock.

The quarantine is a hard link (or an exclusive copy) of the corrupt file, made
before the salvaged cache atomically replaces it, so there is never a moment
without an ``active-sessions.json`` (a failed install leaves the corrupt original
in place for the next locked load to salvage again).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Dict, List

from .cache_schema import SALVAGE_EPOCH_FLOOR, new_cache
from .config import PANE_ID_REGEX, SESSION_ID_REGEX
from .envelopes import is_close_envelope, quarantine, read_json
from .intake import pick_salvage_host
from .log import log_debug
from .markers import remove_pane_marker
from .paths import PRIVATE_FILE_MODE

CANDIDATE_RE = re.compile(r"herdr:[a-zA-Z0-9_-]{1,32}:[a-zA-Z0-9_:-]{1,48}")
PANE_GENERATIONS_RE = re.compile(r'"pane_generations"\s*:\s*\{([^}]*)\}')
GENERATION_PAIR_RE = re.compile(r'"([^"]+)"\s*:\s*(\d+)')
CORRUPT_PREFIX = "active-sessions.json.corrupt."
QUARANTINE_RETENTION_SECONDS = 7 * 86400
QUARANTINE_NAME_ATTEMPTS = 100   # .corrupt.<ts>, then .corrupt.<ts>.1 ... within one second
MARKER_NAME_RE = re.compile(r"^[0-9a-f]+\Z")   # panes/<hex(canonical pane)>: the marker itself, no suffix


def parse_pane_generations(raw_text: str) -> Dict[str, int]:
    """Recover ``pane_generations`` from corrupt text; colon-bearing pane ids parse correctly."""
    match = PANE_GENERATIONS_RE.search(raw_text)
    if not match:
        return {}
    return {
        pane: int(value) for pane, value in GENERATION_PAIR_RE.findall(match.group(1))
        if PANE_ID_REGEX.match(pane)
    }


def candidate_session_ids(raw_text: str) -> List[str]:
    """Anchored-valid candidate ids found anywhere in the raw text (Plan §6.3 step 3), sorted."""
    return sorted({m for m in CANDIDATE_RE.findall(raw_text) if SESSION_ID_REGEX.match(m)})


def salvaged_record(sid: str, generation: int, now: float, now_ns: int) -> dict:
    """The §6.3 step 4 quiescent Idle record for one candidate id."""
    _, host, pane = sid.split(":", 2)
    return {
        "pane_id": pane,
        "workspace_id": pane.split(":", 1)[0] if ":" in pane else None,
        "tab_id": None,
        "host": host,
        "agent": "Herdr",
        "raw_agent": None,
        "title": f"Salvaged Session {pane}",
        "cwd": "",
        "desired_state": "Idle",
        "delivered_state": "Idle",
        "seq": 1,
        "delivered_seq": 1,
        "rejected_seq": 0,
        "generation": generation,
        "desired_payload": {"state": "Idle", "agent": "Herdr", "session_id": sid, "seq": 1},
        "delivery_status": "salvaged",
        "delivery_error": None,
        "delivery_attempts": 0,
        "salvaged": True,
        "admitted_at_ns": now_ns,
        "last_event_ns": now_ns,
        "last_arrival_ns": now_ns,
        "last_event_at": now,
        "last_applied_arrival_time": now,
    }


def build_salvaged_cache(raw_text: str, now: float, now_ns: int, current_host: Callable[[], str]) -> dict:
    """Fresh v4 cache holding every salvageable session under an epoch-dominating generation.

    The root host is pinned to the most common host among the salvaged ids so later
    events map onto the same session ids (gap salvage-host-pin).
    """
    epoch_gen = max(int(now), SALVAGE_EPOCH_FLOOR)
    pane_gens = parse_pane_generations(raw_text)
    sids = candidate_session_ids(raw_text)
    sessions = {}
    for sid in sids:
        pane = sid.split(":", 2)[2]
        generation = max(pane_gens.get(pane, 0), epoch_gen)
        pane_gens = {**pane_gens, pane: generation}
        sessions[sid] = salvaged_record(sid, generation, now, now_ns)
    data = new_cache(pick_salvage_host(sids, current_host()))
    data.update({
        "sessions": sessions,
        "pane_generations": pane_gens,
        "next_generation": max([epoch_gen, *pane_gens.values()]),
    })
    return data


def quarantine_path(state_dir: Path, now: float, attempt: int = 0) -> Path:
    """Plan §6.3 step 1 name ``active-sessions.json.corrupt.<int(now)>``; ``.<attempt>`` avoids same-second collisions."""
    suffix = f".{attempt}" if attempt else ""
    return state_dir / f"{CORRUPT_PREFIX}{int(now)}{suffix}"


def _write_exclusive(path: Path, raw: bytes) -> None:
    """Create ``path`` (0600, O_EXCL) holding ``raw``; raises FileExistsError/OSError, leaving no partial file."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        if fd >= 0:
            os.close(fd)
        discard(path)
        raise


def keep_quarantine_copy(cache_file: Path, raw: bytes, state_dir: Path, now: float) -> Path:
    """Hard-link (or, without link support, copy) the corrupt cache to a free quarantine name; raises OSError.

    The corrupt file itself stays in place until the salvaged cache replaces it atomically.
    """
    for attempt in range(QUARANTINE_NAME_ATTEMPTS):
        path = quarantine_path(state_dir, now, attempt)
        try:
            os.link(str(cache_file), str(path))
            return path
        except FileExistsError:
            continue
        except OSError as e:
            log_debug(f"Hard link to {path.name} failed ({e}); writing a copy instead")
        try:
            _write_exclusive(path, raw)
            return path
        except FileExistsError:
            continue
    raise FileExistsError(f"no free quarantine name for {CORRUPT_PREFIX}{int(now)}")


def discard(path: Path) -> None:
    """Best-effort unlink used on failure paths (the original error is what gets reported)."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log_debug(f"Could not remove {path.name}: {e}")


def _quarantine_stamp(path: Path) -> float:
    stamp = path.name[len(CORRUPT_PREFIX):].split(".", 1)[0]
    return float(stamp) if stamp.isdigit() else path.stat().st_mtime


def prune_corrupt_quarantine(state_dir: Path, now: float) -> List[Path]:
    """Unlink ``active-sessions.json.corrupt.*`` older than 7 days (Plan §6.3 step 1); returns them."""
    removed = []
    for path in sorted(state_dir.glob(f"{CORRUPT_PREFIX}*")):
        try:
            if now - _quarantine_stamp(path) > QUARANTINE_RETENTION_SECONDS:
                path.unlink()
                removed.append(path)
        except FileNotFoundError:
            continue
        except OSError as e:
            log_debug(f"Could not prune quarantined cache {path.name}: {e}")
    return removed


def preserve_close_envelopes(state_dir: Path) -> List[Path]:
    """Plan §6.3 step 2: keep close envelopes (and unparseable ones) in spool/, quarantine status envelopes."""
    spool_dir = state_dir / "spool"
    if not spool_dir.is_dir():
        return []
    moved = []
    for path in sorted(spool_dir.glob("*.json")):
        try:
            env = read_json(path)
        except (OSError, ValueError) as e:
            log_debug(f"Salvage keeps unparseable spool envelope {path.name} (a close cannot be ruled out): {e}")
            continue
        if not is_close_envelope(env) and quarantine(path, spool_dir / "bad", "status envelope during salvage"):
            moved.append(path)
    return moved


def remove_salvaged_markers(data: dict, state_dir: Path) -> None:
    """Plan §6.3 step 4 / §3 L118: every existing pane marker goes (marker + its .failed, nothing else).

    Not only the salvaged panes' markers: a pane whose session id the corrupt text no longer names would
    otherwise keep suppressing its vendor hook for up to 60s with nothing behind it.
    """
    for record in data.get("sessions", {}).values():
        remove_pane_marker(record.get("pane_id"))
    panes = Path(state_dir) / "panes"
    try:
        markers = [p for p in panes.iterdir() if MARKER_NAME_RE.match(p.name)] if panes.is_dir() else []
    except OSError as e:
        log_debug(f"Could not list pane markers for salvage: {e}")
        return
    for marker in markers:
        for path in (marker, marker.with_name(f"{marker.name}.failed")):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                log_debug(f"Could not remove {path.name} on salvage: {e}")
