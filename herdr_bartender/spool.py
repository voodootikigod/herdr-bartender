"""Event spool: contention envelopes (Plan §4.3 Step A contention rule) and FIFO replay under the lock.

* ``defer_event()``: the event path could not take (or save) the cache lock. It
  writes an R6 envelope to ``spool/<enqueued_ns:020d>_<pid>_<monotonic_ns>.json``
  (atomic rename; at most 100 files, close envelopes never pruned), flags the
  reconciler and returns, so the caller exits 0 without touching the cache.
* ``replay_spool_locked(data)``: Step A, while the caller holds the cache lock.
  Up to 16 envelopes in filename (FIFO) order are staged into ``data`` with the
  pure ``staging`` functions; poison envelopes go to ``spool/bad`` (<= 20). The
  caller saves, then calls ``batch.commit()`` (still under the lock) to unlink
  exactly the envelopes whose mutations were saved. Delivery is left to the
  universal sender / reconciler.
"""

from __future__ import annotations

import os
import time  # real monotonic_ns keeps spool filenames unique under a fake clock
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import clock
from .cache import BoundedSessionCache
from .envelopes import (
    build_envelope,
    is_close_envelope,
    quarantine,
    read_json,
    unlink_files,
    validate_envelope,
    write_json_atomic,
)
from .intake import PANE_CLOSED, STATUS_EVENT, container_id, resolve_identity
from .log import log_debug, log_warning
from .paths import ensure_private_dir, get_state_dir
from .process import is_herdr_alive, memoised_herdr_alive
from .handoff import ensure_reconciler_running, touch_reconciler_pending
from .markers import remove_pane_marker
from .staging import stage_container_close, stage_pane_close_result, stage_status

SPOOL_CAP = 100
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


def _make_room(directory: Path) -> None:
    """Keep at most SPOOL_CAP - 1 envelopes before a write; only the oldest non-close ones are dropped."""
    files = sorted(p for p in directory.glob("*.json") if p.is_file())
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


def enqueue_spool(event_name: str, event_data: dict, context: dict, arrival_ns: Optional[int] = None,
                  state_dir: Optional[Path] = None) -> Path:
    """Write one R6 envelope atomically; raises SpoolWriteError."""
    try:
        directory = spool_dir(state_dir)
        _make_room(directory)
        enqueued_ns = clock.time_ns()
        env = build_envelope(event_name, _with_env_workspace(event_data if isinstance(event_data, dict) else {}),
                             context, arrival_ns or enqueued_ns, enqueued_ns)
        path = directory / f"{enqueued_ns:020d}_{os.getpid()}_{time.monotonic_ns()}.json"
        write_json_atomic(path, env)
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
