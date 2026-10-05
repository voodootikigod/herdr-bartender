"""Orphan export file ($HOME/.herdr-bartender-orphans.json): lock, atomic writes, contention journal.

One parser (``parse_orphan_records``) serves the writer and the reader: the canonical
``{"version": 1, "sessions": {...}}``, a bare ``{sid: record}`` mapping and the old
monolith's list form are understood (and normalised on the next write); a file that cannot
be understood raises ``OrphanFileError`` and is never rewritten - the operation waits in the
journal instead. Every rename is made durable (file and parent directory fsynced) before an
export is reported done, so a caller may evict the cached record right after it.

Replay (``--replay-orphans`` and the reconciler's automatic replay) lives in ``replay``.
"""

from __future__ import annotations

import fcntl
import hashlib
import itertools
import json
import os
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, NamedTuple, Optional, Tuple

from .boundedio import (
    DirectoryFull,
    locked_directory,
    JOURNAL_ENTRY_MAX_BYTES,
    ORPHAN_FILE_MAX_BYTES,
    UnusableFile,
    open_exclusive_tmp,
    open_lock_file,
    read_regular_file,
)
from . import clock, jsonsafe
from .envelopes import quarantine
from .log import log_debug, log_warning
from .paths import PRIVATE_FILE_MODE, ensure_private_dir, get_orphan_path
from .handoff import in_reconciler_loop, touch_reconciler_pending


ORPHAN_LOCK_DEADLINE_SECONDS = 0.05   # R10 / Plan §6.1: event-path orphan lock budget
ORPHAN_BLOCKING_DEADLINE_SECONDS = 5.0  # R10: --replay-orphans/--cleanup/reconciler wait, bounded
ORPHAN_LOCK_RETRY_INTERVAL = 0.005
ORPHAN_CAPACITY = 256


def _lock_path(orphan_path: Path) -> Path:
    return orphan_path.with_name(orphan_path.name + ".lock")


def _flock_until(fd: int, deadline: float) -> bool:
    """Retry ``LOCK_EX | LOCK_NB`` on the injectable clock until it succeeds or ``deadline`` seconds pass."""
    give_up_at = clock.monotonic() + deadline
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if clock.monotonic() >= give_up_at:
                return False
            clock.sleep(ORPHAN_LOCK_RETRY_INTERVAL)


def acquire_orphan_lock(orphan_path: Path, blocking: bool = False,
                        deadline: Optional[float] = None) -> Optional[int]:
    """Open and flock the orphan lock file; returns the fd, or None when contended past the deadline.

    R10: the event/plugin path retries LOCK_NB for ``ORPHAN_LOCK_DEADLINE_SECONDS`` (50ms) and
    leaves the work to the reconciler on contention. ``blocking=True`` (--replay-orphans,
    --cleanup, reconciler) waits longer, but never unboundedly: at most
    ``ORPHAN_BLOCKING_DEADLINE_SECONDS`` (every holder keeps the lock for local file I/O only).
    """
    if deadline is None:
        deadline = ORPHAN_BLOCKING_DEADLINE_SECONDS if blocking else ORPHAN_LOCK_DEADLINE_SECONDS
    fd = open_lock_file(_lock_path(orphan_path))
    try:
        if _flock_until(fd, deadline):
            return fd
    except BaseException:
        os.close(fd)
        raise
    os.close(fd)
    log_debug(f"Orphan lock {orphan_path.name}.lock still contended after {deadline:.2f}s")
    return None


def release_orphan_lock(fd: Optional[int]) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError as e:
        log_debug(f"Failed to unlock orphan lock: {e}")
    finally:
        os.close(fd)


class OrphanFileError(Exception):
    """The orphan file could not be locked, read or understood (it is then left untouched)."""


def parse_orphan_records(raw: object) -> Dict[str, object]:
    """``{"version": 1, "sessions": {...}}``, a bare ``{sid: record}`` mapping, or a list of records.

    Raises OrphanFileError for any other shape: a file that cannot be understood is never rewritten or removed.
    """
    if isinstance(raw, dict) and "sessions" in raw:
        if isinstance(raw["sessions"], dict):
            return dict(raw["sessions"])
        raise OrphanFileError("orphan file 'sessions' is not an object")
    if isinstance(raw, list):
        return {(r.get("session_id") if isinstance(r, dict) else None) or f"orphan_{i}": r
                for i, r in enumerate(raw)}
    if isinstance(raw, dict):
        return dict(raw)
    raise OrphanFileError("orphan file is neither an object nor a list")


def read_orphan_records(orphan_path: Path) -> Dict[str, object]:
    """The records of ``orphan_path`` ({} when it does not exist); raises OrphanFileError when unreadable."""
    try:
        raw = jsonsafe.loads(read_regular_file(orphan_path, ORPHAN_FILE_MAX_BYTES))   # R65: bounded
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise OrphanFileError(f"could not read {orphan_path}: {exc}") from exc
    return parse_orphan_records(raw)


_read_orphan_sessions = read_orphan_records   # writer-side name (one parser for writer and reader)


def read_orphan_sids(orphan_file: Optional[Path] = None) -> FrozenSet[str]:
    """Session ids in the orphan file; lock-free (the file is only ever replaced atomically), empty when unreadable."""
    orphan_path = orphan_file or get_orphan_path()
    try:
        return frozenset(read_orphan_records(orphan_path))
    except OrphanFileError as e:
        log_debug(f"Orphan file not readable for its session ids: {e}")
        return frozenset()


class DurabilityError(OSError):
    """A rename could not be made durable (its directory could not be fsynced)."""


def fsync_directory(directory: Path) -> None:
    """Make a rename inside ``directory`` durable; raises DurabilityError when it cannot be (R84).

    Durability is the whole point of the orphan file and its journal (the cache prunes a terminal record only
    once its export is durable), so a directory that cannot be fsynced is a failure, never a silent success:
    the caller then keeps the record owed in the cache.
    """
    try:
        fd = os.open(str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        raise DurabilityError(exc.errno, f"could not open {directory} to fsync it: {exc}") from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise DurabilityError(exc.errno, f"directory fsync of {directory} failed: {exc}") from exc
    finally:
        os.close(fd)


def write_orphan_sessions(orphan_path: Path, sessions: dict) -> None:
    """Atomically and durably replace the orphan file (mode 0600, Plan §8) with ``sessions``."""
    tmp_orphan = orphan_path.with_name(f"{orphan_path.name}.tmp.{os.getpid()}")
    fd = open_exclusive_tmp(tmp_orphan)
    try:
        os.fchmod(fd, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as of:
            fd = None
            json.dump({"version": 1, "sessions": sessions}, of, indent=2)
            of.flush()
            os.fsync(of.fileno())
        os.replace(tmp_orphan, orphan_path)
    except BaseException:
        if fd is not None:
            os.close(fd)
        tmp_orphan.unlink(missing_ok=True)
        raise
    fsync_directory(orphan_path.parent)


# -- contention journal (R10) -------------------------------------------------------
# A contended event-path export/removal must not be lost: it is written as one small
# file under ``<orphan file>.pending/`` (no lock needed: unique name, atomic rename) and
# the reconciler is flagged. Whoever next holds the orphan lock (any export/removal,
# flush_pending_orphan_ops(), --replay-orphans) replays the journal in arrival order
# before its own change, then deletes the replayed entries. Delivery is at-least-once:
# a crash between the rewrite and the deletion replays an entry again, which is harmless
# for exports and removals of Ended records.
OP_EXPORT = "export"
OP_REMOVE = "remove"
_JOURNAL_SEQ = itertools.count()


def pending_dir_for(orphan_path: Path) -> Path:
    return orphan_path.with_name(orphan_path.name + ".pending")


JOURNAL_MAX_ENTRIES = 1024   # R78: distinct sessions with a journaled op (one entry each, the newest)
JOURNAL_KEY_MARK = "-k"          # R78-format entries: <ns>-<pid>-<seq>-<rand>-k<key>.json
JOURNAL_FILE_PREFIX = "k"         # R81-format entries: k<key>.json (sorts after every digit-led legacy name)
JOURNAL_LOCK_SECONDS = 0.05
JOURNAL_COMMIT_LOCK_SECONDS = 1.0   # the commit holds pending/.lock only for local file I/O


def _journal_key(sid: str) -> str:
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()[:32]


def _write_journal_entry(pending: Path, name: str, op: dict) -> Path:
    tmp_path, final_path = pending / f".{name}.tmp", pending / f"{name}.json"
    fd = open_exclusive_tmp(tmp_path)   # a crash-stale tmp of this fixed name is unlinked, never followed
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            json.dump(op, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, final_path)
    except BaseException:
        if fd is not None:
            os.close(fd)
        tmp_path.unlink(missing_ok=True)
        raise
    return final_path


JOURNAL_SEQ_FILE = "seq"


def _next_journal_seq(pending: Path) -> int:
    """Under ``pending/.lock``: the next causal sequence number (a durable counter; gaps after a crash are fine)."""
    path = pending / JOURNAL_SEQ_FILE
    try:
        current = int(read_regular_file(path, 64).decode("ascii").strip() or "0")
    except FileNotFoundError:
        current = 0
    except (OSError, ValueError):
        current = max((_entry_seq(p) for p in pending.glob("*.json")), default=0)
    nxt = current + 1
    fd = open_exclusive_tmp(pending / f".{JOURNAL_SEQ_FILE}.tmp")
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(str(nxt))
        f.flush()
        os.fsync(f.fileno())
    os.replace(pending / f".{JOURNAL_SEQ_FILE}.tmp", path)
    return nxt


def _entry_seq(path: Path) -> int:
    try:
        seq = jsonsafe.loads(read_regular_file(path, JOURNAL_ENTRY_MAX_BYTES)).get("jseq")
    except (OSError, ValueError, AttributeError):
        return 0
    return seq if isinstance(seq, int) and not isinstance(seq, bool) else 0


def _journal_op(orphan_path: Path, op: dict) -> bool:
    """Durably record ``op`` for the next lock holder; False when even that failed (or was refused).

    R78: one entry per session - only the newest op for a session id matters when replayed (an export replaces
    the record, a remove drops it) - so a new op supersedes the older entries of the same session, which are
    removed only once the new one is durable. Under ``pending/.lock`` the journal holds at most
    JOURNAL_MAX_ENTRIES entries; past that (or with the lock busy) the op is refused and the caller keeps the
    record owed in the cache.
    """
    pending = ensure_private_dir(pending_dir_for(orphan_path))
    key = _journal_key(op["sid"])
    # R81: ONE file per session, named by its key and replaced atomically (tmp + rename): there is never a moment
    # with two entries for a session, so no crash or clock step can make an older op replay after a newer one.
    # Older-format entries ("<ns>-<pid>-...") sort before "k<key>" names and replay first, so the new one wins.
    name = f"{JOURNAL_FILE_PREFIX}{key}"
    try:
        with locked_directory(pending, JOURNAL_LOCK_SECONDS):
            current = pending / f"{name}.json"
            legacy = list(pending.glob(f"*{JOURNAL_KEY_MARK}{key}.json"))
            if not current.exists() and not legacy \
                    and sum(1 for _ in pending.glob("*.json")) >= JOURNAL_MAX_ENTRIES:
                log_warning(f"Orphan journal full ({JOURNAL_MAX_ENTRIES} sessions); {op['sid']} stays owed in the cache")
                return False
            _write_journal_entry(pending, name, {**op, "jseq": _next_journal_seq(pending)})
            for path in legacy:
                path.unlink(missing_ok=True)
    except DirectoryFull as exc:
        log_debug(f"Orphan journal busy ({exc}); {op['sid']} stays owed in the cache")
        return False
    fsync_directory(pending)
    if not in_reconciler_loop():   # the reconciler folds the journal every pass: no wake-up for its own entries
        touch_reconciler_pending()
    return True


def _valid_op(op: object) -> bool:
    if not isinstance(op, dict) or not isinstance(op.get("sid"), str):
        return False
    if op.get("op") == OP_EXPORT:
        return isinstance(op.get("session"), dict)
    return op.get("op") == OP_REMOVE


class _Journal(NamedTuple):
    applied: List[Path]   # read and valid: unlinked once their ops are written
    ops: List[dict]       # the valid ops, in arrival order
    poison: List[Path]    # can never be applied (undecodable / malformed): quarantined by the lock holder


def _load_journal(orphan_path: Path) -> _Journal:
    """Read the journal in arrival order (never changes it: lock-free readers use this too).

    An entry that cannot be read right now (OSError, e.g. EIO or EACCES) is in no list: it stays queued for the
    next lock holder, since it may be the only durable copy of an owed Ended export.
    """
    pending = pending_dir_for(orphan_path)
    if not pending.is_dir():
        return _Journal([], [], [])
    journal = _Journal([], [], [])
    for entry in sorted(pending.glob("*.json")):
        try:
            op = jsonsafe.loads(read_regular_file(entry, JOURNAL_ENTRY_MAX_BYTES))
        except UnusableFile as e:   # R65: oversized / not a regular file - never retried forever
            log_debug(f"Orphan journal entry {entry.name} unusable ({e}); quarantining it")
            journal.poison.append(entry)
            continue
        except OSError as e:
            log_debug(f"Orphan journal entry {entry.name} unreadable now ({e}); kept for the next lock holder")
            continue
        except ValueError as e:
            log_debug(f"Orphan journal entry {entry.name} undecodable ({e}); quarantining it")
            journal.poison.append(entry)
            continue
        if _valid_op(op):
            journal.applied.append(entry)
            journal.ops.append(op)
        else:
            log_debug(f"Orphan journal entry {entry.name} malformed; quarantining it")
            journal.poison.append(entry)
    # R81: replay in causal order - legacy entries (no "jseq", already in filename order) first, then by the
    # lock-ordered sequence number every R81 entry carries (never by wall-clock names).
    order = sorted(range(len(journal.ops)), key=lambda i: _replay_rank(journal.ops[i], i))
    return _Journal([journal.applied[i] for i in order], [journal.ops[i] for i in order], journal.poison)


def _replay_rank(op: dict, index: int) -> Tuple[int, int, int]:
    seq = op.get("jseq")
    if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0:
        return (1, seq, index)
    return (0, 0, index)


def journaled_export_sids(orphan_file: Optional[Path] = None) -> FrozenSet[str]:
    """Session ids with an export waiting in the journal (durable, not yet folded into the file)."""
    ops = _load_journal(orphan_file or get_orphan_path()).ops
    return frozenset(op["sid"] for op in ops if op.get("op") == OP_EXPORT)


def journaled_export_records(orphan_file: Optional[Path] = None) -> Dict[str, List[dict]]:
    """R77: the session records each journaled export carries, by session id (several per id are possible)."""
    out: Dict[str, List[dict]] = {}
    for op in _load_journal(orphan_file or get_orphan_path()).ops:
        if op.get("op") == OP_EXPORT and isinstance(op.get("session"), dict):
            out.setdefault(op["sid"], []).append(op["session"])
    return out


def exported_session_records(orphan_file: Optional[Path] = None) -> Dict[str, List[dict]]:
    """R77: every durable exported record (orphan file + journal), by session id; lock-free and read-only.

    Callers must match the RECORD (seq, desired state), not just the id: session ids are deterministic per
    host/pane, so an earlier turn's export can carry the same id.
    """
    orphan_path = orphan_file or get_orphan_path()
    out = journaled_export_records(orphan_path)
    try:
        for sid, record in read_orphan_records(orphan_path).items():
            if isinstance(record, dict):
                out.setdefault(sid, []).append(record)
    except OrphanFileError as e:
        log_debug(f"Orphan file not readable for its records: {e}")
    return out


def _apply_op(sessions: dict, op: dict) -> dict:
    """Pure: ``sessions`` with ``op`` applied (exports keep the newest ORPHAN_CAPACITY).

    An export always lands at the end (file order is export order, R33): a fresh export of a
    session id already in the file replaces the record and makes it the newest (R27).
    """
    sid = op["sid"]
    others = {k: v for k, v in sessions.items() if k != sid}
    if op["op"] == OP_REMOVE:
        return others
    merged = {**others, sid: op["session"]}
    if len(merged) > ORPHAN_CAPACITY:
        return dict(list(merged.items())[-ORPHAN_CAPACITY:])
    return merged


def _commit_locked(orphan_path: Path, ops: List[dict]) -> None:
    """Under the orphan lock: replay the journal, apply ``ops``, write once, drop the journal.

    Raises OrphanFileError, leaving the file AND the journal untouched, when the file cannot be understood.
    R81: the journal is read, applied and pruned under ``pending/.lock`` - the lock every journal writer takes -
    so an entry replaced after it was read can never be unlinked unapplied. Busy past the bound: DirectoryFull.
    """
    pending = pending_dir_for(orphan_path)
    if not pending.is_dir():
        _commit_ops(orphan_path, ops)
        return
    with locked_directory(pending, JOURNAL_COMMIT_LOCK_SECONDS):
        _commit_ops(orphan_path, ops)


def _commit_ops(orphan_path: Path, ops: List[dict]) -> None:
    journal = _load_journal(orphan_path)
    before = _read_orphan_sessions(orphan_path)
    after = before
    for op in [*journal.ops, *ops]:
        after = _apply_op(after, op)
    if not after:
        orphan_path.unlink(missing_ok=True)
    elif list(after.items()) != list(before.items()) or not orphan_path.exists():   # order is data (R33)
        write_orphan_sessions(orphan_path, after)
    for entry in journal.applied:
        entry.unlink(missing_ok=True)
    for entry in journal.poison:   # never silently deleted: the bytes stay for the operator
        quarantine(entry, pending_dir_for(orphan_path) / "bad", "orphan journal entry that cannot be applied")


def _locked_or_journaled(orphan_path: Path, op: dict, blocking: bool) -> bool:
    """Apply ``op`` under the lock (True), or journal it on contention / failure (False)."""
    try:
        lock_fd = acquire_orphan_lock(orphan_path, blocking=blocking)
        if lock_fd is not None:
            try:
                _commit_locked(orphan_path, [op])
                return True
            finally:
                release_orphan_lock(lock_fd)
        log_debug(f"Orphan lock contended; journaling {op['op']} of {op['sid']} for the reconciler")
    except Exception as e:
        log_debug(f"Orphan {op['op']} of {op['sid']} failed ({e}); journaling it for the reconciler")
    try:
        _journal_op(orphan_path, op)
    except Exception as e:
        log_debug(f"Failed to journal orphan {op['op']} of {op['sid']}: {e}")
    return False


def export_orphan_record(sid: str, session_dict: dict, orphan_file: Optional[Path] = None,
                         blocking: bool = False) -> bool:
    """Mirror ``sid`` into the orphan file.

    False when the lock was contended (R10: 50ms LOCK_NB unless ``blocking``) or the write
    failed; the export is then journaled and applied by the next lock holder.
    """
    op = {"op": OP_EXPORT, "sid": sid, "session": session_dict}
    return _locked_or_journaled(orphan_file or get_orphan_path(), op, blocking)


def remove_orphan_record(sid: str, orphan_file: Optional[Path] = None, blocking: bool = False) -> bool:
    """Drop ``sid`` from the orphan file; journaled like an export when it cannot run now (R10)."""
    orphan_path = orphan_file or get_orphan_path()
    if not orphan_path.exists() and not pending_dir_for(orphan_path).is_dir():
        return True
    return _locked_or_journaled(orphan_path, {"op": OP_REMOVE, "sid": sid}, blocking)


def journal_orphan_exports(exports: Iterable[Tuple[str, dict]], orphan_file: Optional[Path] = None) -> None:
    """Durably journal exports WITHOUT taking the orphan lock (so it may run under the cache lock).

    Step A uses it for undelivered Ended records pruned at the 256 cap: the journal entry
    is fsynced before the cache save that prunes the record, so a crash right after that
    save cannot lose the owed Ended (a save that then fails leaves only a harmless duplicate
    mirror of a record still cached). The next orphan-lock holder folds it into the file.
    Raises OSError (or TypeError/ValueError for an unserializable record) on failure.
    """
    orphan_path = orphan_file or get_orphan_path()
    for sid, record in exports:
        if not _journal_op(orphan_path, {"op": OP_EXPORT, "sid": sid, "session": record}):
            raise OSError(f"orphan journal refused the export of {sid}")   # R78: the caller keeps it owed


def run_orphan_io(exports: Iterable[Tuple[str, dict]], removals: Iterable[str], blocking: bool = False) -> None:
    """Staged orphan exports, then removals, OUTSIDE the cache lock (Plan §1 L108); never raises.

    On the event path (``blocking=False``) each operation is bounded by the 50ms
    LOCK_NB orphan lock and otherwise journaled for the reconciler (R10), so callers
    may run it inside ``watchdog.deferred_exit()`` without risking the deadline.
    """
    for sid, record in exports:
        export_orphan_record(sid, record, blocking=blocking)
    for sid in removals:
        remove_orphan_record(sid, blocking=blocking)


def journal_waiting(orphan_path: Path) -> bool:
    """Journaled orphan operations wait to be folded into ``orphan_path``."""
    return _journal_waiting(orphan_path)


def _journal_waiting(orphan_path: Path) -> bool:
    """Journal entries are queued (an unlistable journal counts as waiting, so the flush reports the error)."""
    pending = pending_dir_for(orphan_path)
    try:
        return pending.is_dir() and any(pending.glob("*.json"))
    except OSError as e:
        log_debug(f"Could not list orphan journal {pending}: {e}")
        return True


def fold_journal_locked(orphan_path: Path) -> None:
    """Under the orphan lock: fold queued journal operations into the file (a no-op without a journal).

    Without journal entries the file is left exactly as it is (a hand-written orphan file in a
    legacy shape is never rewritten or removed by a fold).
    """
    if _journal_waiting(orphan_path):
        _commit_locked(orphan_path, [])


def orphan_work_waiting(orphan_path: Optional[Path] = None) -> bool:
    """The orphan file exists or journaled operations wait to be folded into it (reconciler work)."""
    orphan_path = orphan_path or get_orphan_path()
    return orphan_path.exists() or _journal_waiting(orphan_path)


def flush_pending_orphan_ops(orphan_file: Optional[Path] = None, blocking: bool = True,
                             deadline: Optional[float] = None) -> bool:
    """Replay journaled orphan ops into the orphan file (the reconciler calls this each pass).

    True when the journal is empty afterwards; False when the lock was unavailable or I/O failed.
    An empty journal returns at once, without taking (or waiting for) the orphan lock.
    """
    orphan_path = orphan_file or get_orphan_path()
    if not _journal_waiting(orphan_path):
        return True
    try:
        lock_fd = acquire_orphan_lock(orphan_path, blocking=blocking, deadline=deadline)
        if lock_fd is None:
            return False
        try:
            _commit_locked(orphan_path, [])
        finally:
            release_orphan_lock(lock_fd)
        return not _journal_waiting(orphan_path)   # an entry unreadable now stays queued: not drained
    except Exception as e:
        log_debug(f"Failed to flush orphan journal for {orphan_path}: {e}")
        return False


def _pane_ids(records) -> FrozenSet[str]:
    return frozenset(r["pane_id"] for r in records if isinstance(r, dict) and isinstance(r.get("pane_id"), str))


def orphan_pane_ids(orphan_file: Optional[Path] = None) -> Optional[FrozenSet[str]]:
    """Pane ids referenced by the orphan file and its pending journal (Plan §1 L107 prune protection).

    Read under the orphan lock (LOCK_NB, 50ms) and never inside the cache critical
    section; callers pass the result to ``BoundedSessionCache.save(orphan_panes=...)``.
    None when the orphan lock is contended or the read fails (the prune is then deferred).
    """
    orphan_path = orphan_file or get_orphan_path()
    if not orphan_path.exists() and not pending_dir_for(orphan_path).is_dir():
        return frozenset()
    try:
        lock_fd = acquire_orphan_lock(orphan_path)
    except OSError as e:
        log_debug(f"Could not lock {orphan_path} to read orphan panes: {e}")
        return None
    if lock_fd is None:
        return None
    try:
        sessions = _read_orphan_sessions(orphan_path)
        ops = _load_journal(orphan_path).ops
    except OrphanFileError as e:
        log_debug(f"Orphan panes unknown ({e}); prune deferred")
        return None
    finally:
        release_orphan_lock(lock_fd)
    exported = [op["session"] for op in ops if op.get("op") == OP_EXPORT]
    return _pane_ids(list(sessions.values()) + exported)
