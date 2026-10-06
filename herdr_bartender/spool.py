"""Event spool: contention envelopes (Plan §4.3 Step A contention rule) and FIFO replay under the lock.

* ``defer_event()``: the event path could not take (or save) the cache lock. It
  writes an R6 envelope to ``spool/<enqueued_ns:020d>_<pid>_<monotonic_ns>.json``
  (atomic rename; at most 100 status files, oldest pruned; R66: close envelopes are never pruned - the newest close per container replaces older ones, up to 4096 distinct containers; 256 KiB per envelope), flags the
  reconciler and returns, so the caller exits 0 without touching the cache.
* ``replay_spool_locked(data)``: Step A, while the caller holds the cache lock.
  Up to 16 envelopes in filename (FIFO) order are staged into ``data`` with the
  pure ``staging`` functions; poison envelopes go to ``spool/bad`` (<= 20). The
  caller saves, then calls ``batch.commit()`` (still under the lock) to unlink
  exactly the envelopes whose mutations were saved. Delivery is left to the
  universal sender / reconciler.
"""

from __future__ import annotations

import hashlib
import json
import os
import time  # real monotonic_ns keeps spool filenames unique under a fake clock
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import clock
from .cache import BoundedSessionCache
from .boundedio import capped_lock_timeout, locked_directory
from .envelopes import (
    CLOSE_EVENTS,
    MAX_ENVELOPE_BYTES,
    build_envelope,
    is_close_envelope,
    quarantine,
    read_json,
    serialize_json,
    unlink_files,
    validate_envelope,
    write_bytes_atomic,
)
from .intake import PANE_CLOSED, STATUS_EVENT, TAB_CLOSED, WORKSPACE_CLOSED, container_id, resolve_identity
from .log import log_debug, log_warning
from .paths import ensure_private_dir, get_state_dir
from .process import is_herdr_alive, memoised_herdr_alive
from .handoff import ensure_reconciler_running, touch_reconciler_pending
from .markers import remove_pane_marker
from .spool_overflow import close_is_relevant, evictable_close, live_targets
from .staging import stage_container_close, stage_pane_close_result, stage_status

SPOOL_CAP = 100
CLOSE_HARD_CAP = 4096          # R66: distinct containers with a pending close (newest close per container kept)
CLOSE_MARK = "_c"               # keyed close envelopes: <enqueued_ns>_<pid>_<mono>_c<key>.json
CLOSE_KEY_HEX = 32
CLOSE_FIELD_MAX_CHARS = 1024
CLOSE_DATA_KEYS = ("pane_id", "workspace_id", "tab_id", "agent", "agent_status", "state", "timestamp", "title", "cwd")
EXIT_CONTEXT_KEYS = ("focused_pane_id", "focused_pane_agent", "focused_pane_cwd", "workspace_id", "workspace_cwd",
                     "workspace_label", "tab_id")
REPLAY_BATCH = 16
WORKSPACE_ENV = "HERDR_WORKSPACE_ID"


class SpoolWriteError(Exception):
    """The contention envelope could not be written."""


@dataclass(frozen=True)
class ReplayBatch:
    consumed: Tuple[Path, ...] = ()       # applied or superseded: unlink after the save
    quarantined: Tuple[Path, ...] = ()
    staged_sessions: Tuple[str, ...] = ()  # sessions left with an undelivered seq

    @property
    def needs_save(self) -> bool:
        return bool(self.consumed)

    def commit(self) -> None:
        """Unlink the consumed envelopes. Call after a successful save, before releasing the lock."""
        unlink_files(list(self.consumed))


def spool_dir(state_dir: Optional[Path] = None) -> Path:
    return ensure_private_dir((state_dir or get_state_dir()) / "spool")


# -- enqueue ---------------------------------------------------------------------------
def _with_env_workspace(event_data: dict) -> dict:
    """R1: freeze this process's HERDR_WORKSPACE_ID into a colon-less pane event (the replayer's env differs).

    Live intake falls back to it when the event's ``workspace_id`` is missing OR invalid (R1/R2), so both are
    replaced; a valid event workspace is kept.
    """
    pane, ws = event_data.get("pane_id"), os.environ.get(WORKSPACE_ENV)
    if isinstance(pane, str) and ":" not in pane and not container_id(event_data, "workspace_id") and ws:
        return {**event_data, "workspace_id": ws}
    return dict(event_data)


def _is_keyed_close(path: Path) -> bool:
    return CLOSE_MARK in path.stem


def _close_key(event_name: str, event_data: dict) -> str:
    """R66: one key per closed container; a newer close for it supersedes (replaces) the spooled one."""
    if event_name == TAB_CLOSED:
        ids = (event_data.get("tab_id"),)
    elif event_name == WORKSPACE_CLOSED:
        ids = (event_data.get("workspace_id"),)
    else:   # pane.closed or an agent exit (a status event): keyed by pane, in separate namespaces
        ids = (event_data.get("pane_id"), event_data.get("workspace_id"))
    raw = json.dumps([event_name, *[i if isinstance(i, str) else None for i in ids]])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:CLOSE_KEY_HEX]


def _trim(value: object) -> object:
    return value[:CLOSE_FIELD_MAX_CHARS] if isinstance(value, str) else value


def _slim_close(event_name: str, event_data: dict, context: dict) -> Tuple[dict, dict]:
    """R66: a close envelope keeps only the fields its replay reads, each bounded, so it stays a few KiB."""
    data = {k: _trim(event_data[k]) for k in CLOSE_DATA_KEYS if k in event_data}
    if event_name in CLOSE_EVENTS:
        return data, {}   # container closes never consult the context
    return data, {k: _trim(context[k]) for k in EXIT_CONTEXT_KEYS if k in context}


def _make_room(directory: Path) -> None:
    """Keep at most SPOOL_CAP - 1 status envelopes before a write; only the oldest status ones are dropped.

    Keyed close envelopes are never pruned (they have their own ceiling); an unkeyed legacy close is kept too.
    """
    files = sorted(p for p in directory.glob("*.json") if p.is_file() and not _is_keyed_close(p))
    excess = len(files) - (SPOOL_CAP - 1)
    for path in files:
        if excess <= 0:
            return
        try:
            env = read_json(path)
        except (OSError, ValueError) as e:
            excess -= int(quarantine(path, directory / "bad", f"unreadable while pruning: {e}"))
            continue
        if is_close_envelope(env):
            continue
        log_warning(f"Spool at capacity; dropping oldest status envelope {path.name}")
        unlink_files([path])
        excess -= 1


def _spooled_closes(directory: Path, key: str) -> List[Path]:
    return list(directory.glob(f"*{CLOSE_MARK}{key}.json"))


def _require_close_room(directory: Path, superseded: List[Path], event_name: str, event_data: dict) -> None:
    """Under the spool lock: a known container always has room (it replaces itself); a new one needs a slot.

    R83: at the ceiling, a close that can still end something evicts the oldest spooled close that cannot; only a
    close whose replay would be a no-op is refused.
    """
    if superseded:
        return
    keyed = [p for p in directory.glob("*.json") if _is_keyed_close(p)]
    if len(keyed) < CLOSE_HARD_CAP:
        return
    statuses = [p for p in directory.glob("*.json") if not _is_keyed_close(p)]
    targets = live_targets(directory.parent, statuses)
    if not close_is_relevant(event_name, event_data, targets):
        raise SpoolWriteError(f"{CLOSE_HARD_CAP} closes awaiting replay; this one targets nothing cached (no-op)")
    victim = evictable_close(keyed, targets)
    if victim is None:
        raise SpoolWriteError(f"{CLOSE_HARD_CAP} distinct container closes awaiting replay, all still relevant")
    log_warning(f"Close spool at capacity; dropping {victim.name}, whose replay could end nothing")
    unlink_files([victim])


def enqueue_spool(event_name: str, event_data: dict, context: dict, arrival_ns: Optional[int] = None,
                  state_dir: Optional[Path] = None) -> Path:
    """Write one R6 envelope atomically; raises SpoolWriteError.

    R66: status envelopes are capped at SPOOL_CAP (oldest pruned). A close is never pruned and never lost to the
    status cap: a newer close for the same container replaces the spooled one, and distinct pending containers are
    bounded by CLOSE_HARD_CAP (far beyond what Herdr can hold open).
    """
    try:
        directory = spool_dir(state_dir)
        enqueued_ns = clock.time_ns()
        event_data = _with_env_workspace(event_data if isinstance(event_data, dict) else {})
        context = context if isinstance(context, dict) else {}
        env = build_envelope(event_name, event_data, context, arrival_ns or enqueued_ns, enqueued_ns)
        stem = f"{enqueued_ns:020d}_{os.getpid()}_{time.monotonic_ns()}"
        with locked_directory(directory, capped_lock_timeout()):
            superseded: List[Path] = []
            if is_close_envelope(env):
                key = _close_key(event_name, event_data)
                env = build_envelope(event_name, *_slim_close(event_name, event_data, context),
                                     env["arrival_ns"], enqueued_ns)
                superseded = _spooled_closes(directory, key)
                _require_close_room(directory, superseded, event_name, event_data)
                path = directory / f"{stem}{CLOSE_MARK}{key}.json"
            else:
                _make_room(directory)
                path = directory / f"{stem}.json"
            data = serialize_json(env)
            if len(data) > MAX_ENVELOPE_BYTES:
                raise SpoolWriteError(f"envelope too large ({len(data)} bytes > {MAX_ENVELOPE_BYTES})")
            write_bytes_atomic(path, data)
            unlink_files(superseded)   # R67: only once the replacement is durable
        return path
    except (OSError, TypeError, ValueError) as e:
        raise SpoolWriteError(f"could not spool {event_name}: {e}") from e


def defer_event(event_name: str, event_data: dict, context: dict, arrival_ns: int, reason: object) -> Optional[Path]:
    """Contention path: spool the event, flag and ensure the reconciler; never raises, never touches the cache."""
    path = None
    try:
        path = enqueue_spool(event_name, event_data, context, arrival_ns)
        log_debug(f"Deferred {event_name} to spool/{path.name} ({reason})")
    except SpoolWriteError as e:
        log_warning(f"{event_name} lost: {reason}; {e}")
    touch_reconciler_pending()
    ensure_reconciler_running()
    return path


# -- replay ----------------------------------------------------------------------------
def apply_envelope_locked(data: dict, env: dict, herdr_alive: Callable[[], bool]) -> Tuple[str, ...]:
    """Stage one validated envelope into ``data``; returns the session ids it staged (none: dropped)."""
    name, event_data, arrival_ns = env["event_name"], env["event_data"], env["arrival_ns"]
    context = env.get("context") or {}
    generation = env.get("generation")
    if name in (STATUS_EVENT, PANE_CLOSED):
        identity, notes = resolve_identity(event_data, context if name == STATUS_EVENT else {}, env={})
        for note in notes:
            log_debug(note)
        if identity is None:
            return ()
        if name == PANE_CLOSED:
            closed = stage_pane_close_result(data, identity.canonical_pane, event_data, arrival_ns, generation,
                                             herdr_alive=herdr_alive)
            if closed.recorded:   # Plan §1 L59: the marker goes under the close's lock
                remove_pane_marker(identity.canonical_pane)
            return tuple(t.session_id for t in closed.targets)
        stage = stage_status(data, identity, event_data, context, arrival_ns, herdr_alive=herdr_alive,
                             spool_generation=generation, require_newer=True)
        return (stage.session_id,) if stage.staged else ()
    return tuple(t.session_id for t in stage_container_close(data, name, event_data, arrival_ns, generation,
                                                             herdr_alive=herdr_alive))


def _replay_one(data: dict, path: Path, bad_dir: Path, herdr_alive: Callable[[], bool]) -> Optional[Tuple[str, ...]]:
    """Staged session ids, or None when the envelope was quarantined."""
    try:
        env = read_json(path)
    except (OSError, ValueError) as e:
        quarantine(path, bad_dir, f"undecodable envelope: {e}")
        return None
    reason = validate_envelope(env)
    if reason:
        quarantine(path, bad_dir, reason)
        return None
    try:
        return apply_envelope_locked(data, env, herdr_alive)
    except Exception as e:  # a poison pill must never block the FIFO
        quarantine(path, bad_dir, f"replay failed: {e!r}")
        return None


def replay_spool_locked(data: dict, state_dir: Optional[Path] = None, max_batch: int = REPLAY_BATCH,
                        herdr_alive: Callable[[], bool] = memoised_herdr_alive) -> ReplayBatch:
    """Plan §4.3 Step A spool replay; the caller holds the cache lock, then saves and commits.

    ``herdr_alive`` (tombstone gate) must not spawn under the lock: callers warm the
    0.5s liveness memo before locking (Plan §6.1).
    """
    directory = (state_dir or get_state_dir()) / "spool"
    if not directory.is_dir():
        return ReplayBatch()
    consumed: List[Path] = []
    quarantined: List[Path] = []
    staged: List[str] = []
    for path in sorted(p for p in directory.glob("*.json") if p.is_file())[:max_batch]:
        result = _replay_one(data, path, directory / "bad", herdr_alive)
        if result is None:
            quarantined.append(path)
        else:
            consumed.append(path)
            staged.extend(result)
    return ReplayBatch(tuple(consumed), tuple(quarantined), tuple(dict.fromkeys(staged)))


def replay_spool_dir(state_dir: Path, max_batch: int = REPLAY_BATCH,
                     cache_mgr: Optional[BoundedSessionCache] = None) -> ReplayBatch:
    """Reconciler step 1b: one locked replay pass (raises CacheError on lock/save failure)."""
    directory = Path(state_dir) / "spool"
    if not directory.is_dir() or not any(directory.glob("*.json")):
        return ReplayBatch()
    cache_mgr = cache_mgr or BoundedSessionCache(state_dir)
    is_herdr_alive()  # resolve (memoise) before locking: no subprocess under the cache lock
    with cache_mgr as data:
        batch = replay_spool_locked(data, Path(state_dir), max_batch)
        if batch.needs_save:
            cache_mgr.save(data)
            batch.commit()
    return batch
