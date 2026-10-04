"""Orphan export file ($HOME/.herdr-bartender-orphans.json) and replay."""

from __future__ import annotations

import fcntl
import itertools
import json
import os
from pathlib import Path
from typing import List, Optional, Tuple

from . import clock, runtime
from .bridge import post_bartender_event
from .cache import BoundedSessionCache
from .config import SESSION_ID_REGEX
from .log import log_debug
from .markers import remove_pane_marker
from .paths import PRIVATE_FILE_MODE, ensure_private_dir, get_orphan_path, get_state_dir
from .sender import touch_reconciler_pending


ORPHAN_LOCK_DEADLINE_SECONDS = 0.05   # R10 / Plan §6.1: event-path orphan lock budget
ORPHAN_LOCK_RETRY_INTERVAL = 0.005
ORPHAN_CAPACITY = 256


def _lock_path(orphan_path: Path) -> Path:
    return orphan_path.with_name(orphan_path.name + ".lock")


def acquire_orphan_lock(orphan_path: Path, blocking: bool = False,
                        deadline: float = ORPHAN_LOCK_DEADLINE_SECONDS) -> Optional[int]:
    """Open and flock the orphan lock file; returns the fd, or None when contended past ``deadline``.

    R10: the event/plugin path uses LOCK_NB retried for ``deadline`` seconds (50ms) and
    leaves the work to the reconciler on contention. ``blocking=True`` (--replay-orphans,
    --cleanup, reconciler) waits for the lock.
    """
    fd = os.open(str(_lock_path(orphan_path)), os.O_CREAT | os.O_RDWR, PRIVATE_FILE_MODE)
    try:
        if blocking:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return fd
        give_up_at = clock.monotonic() + deadline
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if clock.monotonic() >= give_up_at:
                    break
                clock.sleep(ORPHAN_LOCK_RETRY_INTERVAL)
    except BaseException:
        os.close(fd)
        raise
    os.close(fd)
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


def _read_orphan_sessions(orphan_path: Path) -> dict:
    if not orphan_path.exists():
        return {}
    try:
        with open(orphan_path, "r", encoding="utf-8") as of:
            data = json.load(of)
    except (OSError, ValueError) as e:
        log_debug(f"Unreadable orphan file {orphan_path}: {e}")
        return {}
    sessions = data.get("sessions", {}) if isinstance(data, dict) else {}
    return sessions if isinstance(sessions, dict) else {}


def write_orphan_sessions(orphan_path: Path, sessions: dict) -> None:
    """Atomically replace the orphan file (mode 0600, Plan §8) with ``sessions``."""
    tmp_orphan = orphan_path.with_name(f"{orphan_path.name}.tmp.{os.getpid()}")
    fd = os.open(str(tmp_orphan), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
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


def _journal_op(orphan_path: Path, op: dict) -> bool:
    """Durably record ``op`` for the next lock holder; False when even that failed."""
    pending = ensure_private_dir(pending_dir_for(orphan_path))
    name = f"{clock.time_ns():020d}-{os.getpid()}-{next(_JOURNAL_SEQ):06d}-{os.urandom(3).hex()}"
    tmp_path, final_path = pending / f".{name}.tmp", pending / f"{name}.json"
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
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
    touch_reconciler_pending()
    return True


def _valid_op(op: object) -> bool:
    if not isinstance(op, dict) or not isinstance(op.get("sid"), str):
        return False
    if op.get("op") == OP_EXPORT:
        return isinstance(op.get("session"), dict)
    return op.get("op") == OP_REMOVE


def _load_journal(orphan_path: Path) -> Tuple[List[Path], List[dict]]:
    """(entry paths, valid ops) in arrival order; unreadable entries are logged and dropped."""
    pending = pending_dir_for(orphan_path)
    if not pending.is_dir():
        return [], []
    entries = sorted(pending.glob("*.json"))
    ops = []
    for entry in entries:
        try:
            op = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log_debug(f"Dropping unreadable orphan journal entry {entry.name}: {e}")
            continue
        if _valid_op(op):
            ops.append(op)
        else:
            log_debug(f"Dropping malformed orphan journal entry {entry.name}")
    return entries, ops


def _apply_op(sessions: dict, op: dict) -> dict:
    """Pure: ``sessions`` with ``op`` applied (exports keep the newest ORPHAN_CAPACITY)."""
    sid = op["sid"]
    if op["op"] == OP_REMOVE:
        return {k: v for k, v in sessions.items() if k != sid}
    merged = {**sessions, sid: op["session"]}
    if len(merged) > ORPHAN_CAPACITY:
        return dict(list(merged.items())[-ORPHAN_CAPACITY:])
    return merged


def _commit_locked(orphan_path: Path, ops: List[dict]) -> None:
    """Under the orphan lock: replay the journal, apply ``ops``, write once, drop the journal."""
    entries, journaled = _load_journal(orphan_path)
    before = _read_orphan_sessions(orphan_path)
    after = before
    for op in [*journaled, *ops]:
        after = _apply_op(after, op)
    if not after:
        orphan_path.unlink(missing_ok=True)
    elif after != before or not orphan_path.exists():
        write_orphan_sessions(orphan_path, after)
    for entry in entries:
        entry.unlink(missing_ok=True)


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


def flush_pending_orphan_ops(orphan_file: Optional[Path] = None, blocking: bool = True) -> bool:
    """Replay journaled orphan ops into the orphan file (the reconciler calls this each pass).

    True when the journal is empty afterwards; False when the lock was unavailable or I/O failed.
    """
    orphan_path = orphan_file or get_orphan_path()
    if not pending_dir_for(orphan_path).is_dir():
        return True
    try:
        lock_fd = acquire_orphan_lock(orphan_path, blocking=blocking)
        if lock_fd is None:
            return False
        try:
            _commit_locked(orphan_path, [])
            return True
        finally:
            release_orphan_lock(lock_fd)
    except Exception as e:
        log_debug(f"Failed to flush orphan journal for {orphan_path}: {e}")
        return False


def run_replay_orphans(orphan_path_str: str, bridge_url: str | None = None) -> bool:
    orphan_path = Path(orphan_path_str).expanduser()
    if not orphan_path.exists():
        print(f"[-] Orphan file not found: {orphan_path}")
        return False

    # Plan L619/L686: replay is exempt from the 1.5s event-path watchdog.
    runtime.set_deadline_mode(runtime.DEADLINE_UNBOUNDED)
    lock_fd = None
    try:
        # R10: --replay-orphans may block on the orphan lock (bounded by its own budget).
        lock_fd = acquire_orphan_lock(orphan_path, blocking=True)
        try:
            _commit_locked(orphan_path, [])  # fold in exports journaled under contention
        except Exception as e:
            log_debug(f"Failed to fold orphan journal into {orphan_path}: {e}")

        try:
            with open(orphan_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[-] Failed to read orphan file: {e}")
            return False

        sessions = data.get("sessions", {}) if isinstance(data, dict) else {}
        if not sessions and isinstance(data, list):
            sessions = {s.get("session_id", f"orphan_{i}"): s for i, s in enumerate(data)}
        elif not sessions and isinstance(data, dict):
            sessions = data

        cache_mgr = BoundedSessionCache(get_state_dir())
        with cache_mgr as cdata:
            active_sessions = cdata.get("sessions", {})
            pane_gens = cdata.get("pane_generations", {})

        print(f"[*] Replaying {len(sessions)} orphaned session(s) from {orphan_path}...")
        all_success = True
        for sid, s in list(sessions.items()):
            if not isinstance(sid, str) or not SESSION_ID_REGEX.match(sid):
                print(f"    [-] Skipping orphan with invalid session_id: {sid}")
                sessions.pop(sid, None)
                continue

            orphan_gen = s.get("generation", 0) if isinstance(s, dict) else 0
            pane_id = s.get("pane_id") if isinstance(s, dict) else None
            if not pane_id and ":" in sid:
                parts = sid.split(":")
                pane_id = ":".join(parts[2:]) if len(parts) >= 3 else parts[-1]

            curr_gen = pane_gens.get(pane_id, 0)
            active_s = active_sessions.get(sid)
            if not active_s and pane_id:
                for asid, ainfo in active_sessions.items():
                    if ainfo.get("pane_id") == pane_id:
                        active_s = ainfo
                        break
            # Single normative boolean predicate:
            # Skip orphan iff an active, live (non-salvaged) session exists on the pane with desired_state != 'Ended'
            if active_s and not active_s.get("salvaged", False) and active_s.get("desired_state") != "Ended":
                print(f"    [*] Skipping orphan {sid}: pane currently has active non-Ended session {active_s.get('desired_state')}")
                sessions.pop(sid, None)
                continue

            agent_name = s.get("agent") if isinstance(s, dict) else "Herdr"
            payload = {
                "state": "Ended",
                "agent": agent_name or "Herdr",
                "session_id": sid,
            }
            success, is_non_retryable = post_bartender_event(payload, timeout=0.2, bridge_url=bridge_url)
            if success:
                print(f"    [+] Cleared {sid}")
                sessions.pop(sid, None)
                if pane_id:
                    remove_pane_marker(pane_id)
                with cache_mgr as cdata:
                    active_s = cdata.get("sessions", {}).get(sid)
                    if not active_s and pane_id:
                        for asid, ainfo in cdata.get("sessions", {}).items():
                            if ainfo.get("pane_id") == pane_id:
                                active_s = ainfo
                                break
                    if active_s and active_s.get("desired_state") != "Ended":
                        active_s["delivered_seq"] = 0
                        active_s["delivery_status"] = "in_flight"
                        touch_reconciler_pending()
                        cdata["consecutive_failures"] = 0
                        cache_mgr.save(cdata)
            else:
                print(f"    [-] Failed to deliver Ended for {sid}")
                all_success = False

        if all_success and not sessions:
            try:
                orphan_path.unlink()
                print(f"[+] Successfully cleared all orphaned sessions; removed {orphan_path}")
            except Exception:
                pass
        elif not all_success:
            try:
                write_orphan_sessions(orphan_path, sessions)
            except Exception as e:
                log_debug(f"Failed to rewrite remaining orphans to {orphan_path}: {e}")
        return all_success
    finally:
        release_orphan_lock(lock_fd)
