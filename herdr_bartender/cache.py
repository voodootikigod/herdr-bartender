"""Locked session cache (active-sessions.json): bounded lock, atomic save, schema v4, salvage.

Contract (Plan §4.3, §6.1-§6.3; gaps lock-best-effort, save-errors-swallowed,
in-critical-section-flag, deferred-exit-only-in-save):

* ``with cache as data`` takes ``LOCK_EX`` within ``effective_lock_timeout()``
  seconds or raises ``LockTimeout``; it never proceeds without the lock.
* ``runtime.IN_CRITICAL_SECTION`` is True exactly while the flock is held.
* ``save(data)`` writes tmp + fsync + replace while the lock is held and raises
  ``CacheWriteError`` on any failure (no tmp file is left behind). Callers must
  not send network traffic for state that was not saved.
* Leaving the with-block (normally, by early return or by exception) unlocks
  first, then honours a SIGALRM deferred during the critical section
  (``watchdog.honor_pending_exit``: reconciler hand-off, exit 0), unless an
  enclosing ``watchdog.deferred_exit()`` section is still open (the event is not
  yet saved or spooled); that section honours it when it ends.
* A corrupt cache is salvaged under the lock without ever leaving the state dir
  without an ``active-sessions.json`` (``_install_salvaged``).
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import FrozenSet, List, Optional

from . import clock, jsonsafe, runtime, watchdog
from .cache_schema import CorruptCache, new_cache, normalize_cache
from .config import get_sanitized_hostname
from .housekeeping import sweep_stale_temp_files
from .log import log_debug, log_warning
from .paths import PRIVATE_FILE_MODE
from .salvage import (
    build_salvaged_cache,
    discard,
    keep_quarantine_copy,
    preserve_close_envelopes,
    prune_corrupt_quarantine,
    remove_salvaged_markers,
)

CACHE_FILE_NAME = "active-sessions.json"
LOCK_FILE_NAME = "active-sessions.lock"
LOCK_RETRY_INTERVAL = 0.01
BOUNDED_LOCK_CAP = 0.2          # Plan §4.3 L390 / R9
BOUNDED_LOCK_FLOOR = 0.02
LOCK_BUDGET_RESERVE = 0.3
UNBOUNDED_LOCK_TIMEOUT = 5.0    # R10: reconciler/--cleanup may wait longer, bounded by their own budget
SESSION_CAP = 256
PANE_GENERATION_CAP = 512
TOMBSTONE_TTL_NS = 60_000_000_000
AGENT_EXIT_TTL_NS = 60_000_000_000   # Plan §4.1 L303: agent_exits entries are pruned after 60s
# An ``orphaned_ended`` record whose orphan export could not be made durable before the marking save (journal and
# file both failed): the cache holds the only copy of the unconfirmed Ended, so the cap prune must keep it.
ORPHAN_MIRROR_OWED = "orphan_mirror_owed"


class CacheError(Exception):
    """Base class: the cache could not be locked, read or written; nothing was changed."""


class LockTimeout(CacheError):
    """The cache lock was not acquired within the deadline; nothing was read or written."""


class CacheReadError(CacheError):
    """active-sessions.json exists but could not be read (I/O error, not corruption)."""


class CacheWriteError(CacheError):
    """The cache could not be persisted; the previous file is intact and no tmp file remains."""


class IntegrationDisabled(Exception):
    """DISABLED was present when re-checked under the lock (``check_disabled=True``)."""


def lock_timeout_seconds() -> float:
    """Bounded path: ``min(0.2, max(0.02, time_remaining() - 0.3))``; unbounded callers wait longer."""
    if not runtime.deadline_bounded():
        return UNBOUNDED_LOCK_TIMEOUT
    return min(BOUNDED_LOCK_CAP, max(BOUNDED_LOCK_FLOOR, runtime.time_remaining() - LOCK_BUDGET_RESERVE))


def _flock_within(fd: int, timeout: float) -> bool:
    give_up_at = clock.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if clock.monotonic() >= give_up_at:
                return False
            clock.sleep(LOCK_RETRY_INTERVAL)


def write_cache_file(cache_file: Path, data: dict) -> None:
    """tmp (``<name>.tmp.<pid>``) + flush + fsync + ``os.replace`` (Plan §6.2); raises CacheWriteError."""
    tmp_path = cache_file.with_name(f"{cache_file.name}.tmp.{os.getpid()}")
    fd = -1
    try:
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, cache_file)
    except (OSError, TypeError, ValueError) as e:
        if fd >= 0:
            os.close(fd)
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise CacheWriteError(f"could not write {cache_file.name}: {e}") from e


def pane_generations_to_prune(pane_gens: dict, protected: FrozenSet[str],
                              cap: int = PANE_GENERATION_CAP) -> List[str]:
    """Lowest-generation unprotected panes to drop so at most ``cap`` remain (Plan §5.1 item 12)."""
    if len(pane_gens) <= cap:
        return []
    prunable = sorted((gen, pane) for pane, gen in pane_gens.items() if pane not in protected)
    return [pane for _, pane in prunable[:len(pane_gens) - cap]]


def _prune_tombstones(tombstones: dict, now_ns: int) -> dict:
    def closed_ns(entry: object) -> int:
        if isinstance(entry, dict):
            return int(entry.get("closed_at_ns", 0) or 0)
        return int(entry) if isinstance(entry, (int, float)) else 0
    return {pane: e for pane, e in tombstones.items() if now_ns - closed_ns(e) <= TOMBSTONE_TTL_NS}


def _prune_agent_exits(agent_exits: dict, now_ns: int) -> dict:
    def exit_ns(entry: object) -> int:
        return int(entry.get("exit_at_ns", 0) or 0) if isinstance(entry, dict) else 0
    return {pane: e for pane, e in agent_exits.items() if now_ns - exit_ns(e) <= AGENT_EXIT_TTL_NS}


def safe_to_evict(record: dict) -> bool:
    """Salvaged, or an Ended that Bartender confirmed or that is mirrored to the orphan file.

    ``orphaned_ended`` alone means "mirrored" only when its export was durable when it was marked; a record flagged
    ``ORPHAN_MIRROR_OWED`` waits for the R12 horizon eviction (export confirmed first) or a journaling Step A prune.
    """
    if record.get("salvaged", False):
        return True
    return record.get("desired_state") == "Ended" and (
        (record.get("delivered_state") == "Ended" and not _seq_behind(record))
        or _seq_confirmed(record)
        or (bool(record.get("orphaned_ended", False)) and not record.get(ORPHAN_MIRROR_OWED, False))
    )


def _seq_behind(record: dict) -> bool:
    """A newer seq is known undelivered: ``delivered_state`` then describes an older send (e.g. an old Ended's late
    success after a newer turn and Ended were staged), not the current Ended."""
    seq, delivered = record.get("seq"), record.get("delivered_seq")
    return all(isinstance(v, int) and not isinstance(v, bool) for v in (seq, delivered)) and delivered < seq


def _seq_confirmed(record: dict) -> bool:
    """``delivered_seq == seq`` with both real integers: two missing (or dropped, R55) fields are no evidence."""
    seq, delivered = record.get("seq"), record.get("delivered_seq")
    return all(isinstance(v, int) and not isinstance(v, bool) for v in (seq, delivered)) and seq == delivered


def mirror_copy(record: dict, **overrides) -> dict:
    """The orphan-file copy of a cached record: its cache-only ``ORPHAN_MIRROR_OWED`` flag is not mirrored."""
    return {**{k: v for k, v in record.items() if k != ORPHAN_MIRROR_OWED}, **overrides}


def event_time(record: object) -> float:
    """Total sort key for "oldest first": ``last_event_at`` when it is a number, else 0 (null/missing sort oldest)."""
    value = record.get("last_event_at") if isinstance(record, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def sessions_to_prune(sessions: dict, cap: int = SESSION_CAP) -> List[str]:
    """256 cap: only Ended-safe or salvaged records are evictable, oldest first (Plan §4.1)."""
    if len(sessions) <= cap:
        return []
    oldest_first = sorted(sessions.items(), key=lambda kv: event_time(kv[1]))
    return [sid for sid, rec in oldest_first if safe_to_evict(rec)][:len(sessions) - cap]


def prepare_for_save(data: dict, now: float, now_ns: int, orphan_panes: Optional[FrozenSet[str]]) -> None:
    """Root bookkeeping applied to ``data`` in place before every save (callers keep using ``data``)."""
    data["last_updated"] = now
    data["cache_seq"] = int(data.get("cache_seq", 0) or 0) + 1
    data.setdefault("host", get_sanitized_hostname())
    data["tombstones"] = _prune_tombstones(data.get("tombstones", {}), now_ns)
    data["agent_exits"] = _prune_agent_exits(data.get("agent_exits", {}), now_ns)
    sessions = data.setdefault("sessions", {})
    for sid in sessions_to_prune(sessions):
        sessions.pop(sid, None)  # in place: callers may hold a reference to data["sessions"]
    if orphan_panes is not None:
        protected = frozenset(
            {s.get("pane_id") for s in sessions.values()}
            | set(data["tombstones"]) | set(data.get("agent_exits", {})) | set(orphan_panes)
        )
        pane_gens = data.setdefault("pane_generations", {})
        for pane in pane_generations_to_prune(pane_gens, protected):
            pane_gens.pop(pane, None)


class BoundedSessionCache:
    """Process-safe session cache with a bounded non-blocking flock and quarantine/salvage."""

    def __init__(self, state_dir: Path, lock_timeout: Optional[float] = None, check_disabled: bool = False):
        self.state_dir = Path(state_dir)
        self.cache_file = self.state_dir / CACHE_FILE_NAME
        self.lock_file = self.state_dir / LOCK_FILE_NAME
        self.lock_timeout = lock_timeout
        self.check_disabled = check_disabled
        self._lock_fd: Optional[int] = None
        sweep_stale_temp_files(self.state_dir)

    @property
    def held(self) -> bool:
        return self._lock_fd is not None

    def effective_lock_timeout(self) -> float:
        return self.lock_timeout if self.lock_timeout is not None else lock_timeout_seconds()

    # -- lock lifecycle --------------------------------------------------------
    def __enter__(self) -> dict:
        if self._lock_fd is not None:
            raise RuntimeError("BoundedSessionCache is not re-entrant")
        self._acquire()
        try:
            if self.check_disabled and (self.state_dir / "DISABLED").exists():
                raise IntegrationDisabled("DISABLED present under the cache lock")
            return self._load()
        except BaseException:
            self._release()
            raise

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._release()
        return False

    def _acquire(self) -> None:
        timeout = self.effective_lock_timeout()
        try:
            fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR, PRIVATE_FILE_MODE)
        except OSError as e:
            raise CacheReadError(f"cannot open {self.lock_file.name}: {e}") from e
        try:
            if not _flock_within(fd, timeout):
                raise LockTimeout(f"cache lock not acquired within {timeout:.3f}s")
            runtime.IN_CRITICAL_SECTION = True
            self._lock_fd = fd
        except BaseException:
            if self._lock_fd is None:
                os.close(fd)
                runtime.IN_CRITICAL_SECTION = False
            raise

    def _release(self) -> None:
        fd, self._lock_fd = self._lock_fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError as e:
            log_debug(f"Cache unlock failed (closing the fd releases it): {e}")
        finally:
            try:
                os.close(fd)
            finally:
                runtime.IN_CRITICAL_SECTION = False
        watchdog.honor_pending_exit()

    # -- load / save ------------------------------------------------------------
    def _load(self) -> dict:
        try:
            raw = self.cache_file.read_bytes()
        except FileNotFoundError:
            return new_cache(get_sanitized_hostname())
        except OSError as e:
            raise CacheReadError(f"could not read {self.cache_file.name}: {e}") from e
        try:
            return normalize_cache(jsonsafe.loads(raw), get_sanitized_hostname)
        except (ValueError, TypeError, CorruptCache) as e:   # TypeError: a shape no normalizer anticipated
            log_warning(f"Quarantining corrupt cache: {e}")
            return self._salvage(raw)

    def _salvage(self, raw: bytes) -> dict:
        """Plan §6.3: persist the salvaged cache under the held lock, then fix up spool and markers."""
        now, now_ns = clock.time(), clock.time_ns()
        salvaged = build_salvaged_cache(raw.decode("utf-8", errors="ignore"), now, now_ns, get_sanitized_hostname)
        prepare_for_save(salvaged, now, now_ns, None)
        self._install_salvaged(salvaged, raw, now)
        prune_corrupt_quarantine(self.state_dir, now)
        preserve_close_envelopes(self.state_dir)
        remove_salvaged_markers(salvaged, self.state_dir)
        log_warning(f"Salvaged {len(salvaged['sessions'])} session(s) from a corrupt cache")
        return salvaged

    def _install_salvaged(self, salvaged: dict, raw: bytes, now: float) -> None:
        """Plan §6.3 steps 1 + 6 with no window lacking a cache file (raises CacheWriteError).

        Write the salvaged cache aside, keep a quarantine copy (hard link) of the corrupt
        file, then ``os.replace`` the corrupt file in one atomic step. On any failure the
        corrupt original stays as active-sessions.json and the quarantine copy is removed,
        so the next locked load salvages again (generations stay epoch-dominating).
        """
        staged = self.cache_file.with_name(f"{self.cache_file.name}.salvage.{os.getpid()}")
        write_cache_file(staged, salvaged)
        quarantined: Optional[Path] = None
        try:
            quarantined = keep_quarantine_copy(self.cache_file, raw, self.state_dir, now)
            os.replace(staged, self.cache_file)
        except OSError as e:
            discard(staged)
            if quarantined is not None:
                discard(quarantined)
            raise CacheWriteError(f"could not install salvaged cache: {e}") from e

    def save(self, data: dict, orphan_panes: Optional[FrozenSet[str]] = None) -> None:
        """Atomically persist ``data`` (updated in place: cache_seq, prunes) while holding the lock.

        ``orphan_panes`` (pane ids referenced by the orphan file, read by the caller
        outside the lock) enables the 512-entry pane_generations prune; without it the
        prune is deferred. Raises CacheWriteError; the old cache stays intact.
        """
        if self._lock_fd is None:
            raise CacheWriteError("save() requires the cache lock")
        prepare_for_save(data, clock.time(), clock.time_ns(), orphan_panes)
        write_cache_file(self.cache_file, data)
