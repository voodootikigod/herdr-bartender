"""Reconciler code-version stamp: spot a reconciler from older code still holding ``reconciler.lock`` (R38).

An in-place upgrade (``git pull`` in the symlinked repo) does not touch a reconciler that is already
running: it keeps its old code in memory and keeps the singleton lock while any session is live, so
every new spawn defers to it. Each reconciler of this code therefore writes ``reconciler.stamp``
(``{"version", "pid", "start_time"}``, 0600) right after taking the lock and removes it on release.
A held lock whose stamp is missing, from another version, or from a process that is gone belongs to
an older reconciler: ``--status`` and the startup hook warn with the stop command (README "Upgrading").

R43: the version is ``"<CODE_SCHEMA>:<digest>"``, the digest a SHA-256 over this package's sources (every ``*.py``
plus ``hook_guard.sh``), so any package-to-package upgrade is a new version, not only the monolith one.
A reconciler computes it once, before taking the lock (the code it runs was just loaded from disk).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import List, Optional

from . import clock, jsonsafe
from .handoff import RECONCILER_LOCK_NAME, reconciler_running
from .log import log_debug
from .paths import PRIVATE_FILE_MODE, get_state_dir
from .process import is_process_instance_alive, own_start_time

STAMP_FILE_NAME = "reconciler.stamp"
CODE_SCHEMA = 2                      # 1: the pre-package monolith (it never wrote a stamp)
CODE_ROOT = Path(__file__).resolve().parent
_code_version: Optional[str] = None
STAMP_SETTLE_SECONDS = 0.2           # a reconciler that just took the lock writes its stamp right after
STOP_COMMAND = 'pkill -u "$(id -u)" -f "herdr-bartender --reconcile-background"'


def _code_files(root: Path) -> List[Path]:
    return sorted(p for p in root.rglob("*") if (p.suffix == ".py" or p.name == "hook_guard.sh")
                  and "__pycache__" not in p.parts and p.is_file())


def code_version() -> str:
    """This process's code version (memoised): the schema and a digest of the package sources (R38)."""
    global _code_version
    if _code_version is None:
        digest = hashlib.sha256()
        for path in _code_files(CODE_ROOT):
            digest.update(path.relative_to(CODE_ROOT).as_posix().encode("utf-8") + b"\0")
            try:
                digest.update(path.read_bytes())
            except OSError as exc:
                digest.update(f"unreadable:{type(exc).__name__}".encode("utf-8"))
            digest.update(b"\0")
        _code_version = f"{CODE_SCHEMA}:{digest.hexdigest()[:16]}"
    return _code_version


def _stamp_path(state_dir: Optional[Path]) -> Path:
    return (Path(state_dir) if state_dir is not None else get_state_dir()) / STAMP_FILE_NAME


def write_stamp(state_dir: Optional[Path] = None) -> None:
    """Record this process as the lock holder (call right after taking ``reconciler.lock``)."""
    path = _stamp_path(state_dir)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    record = {"version": code_version(), "pid": os.getpid(), "start_time": own_start_time()}
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(tmp, path)
    except OSError as exc:
        log_debug(f"Could not write {STAMP_FILE_NAME}: {exc}")
        try:
            tmp.unlink()
        except OSError:
            pass


def clear_stamp(state_dir: Optional[Path] = None) -> None:
    """Remove our own stamp (before releasing the lock); a newer holder's stamp is left alone."""
    path = _stamp_path(state_dir)
    try:
        record = jsonsafe.loads(path.read_text(encoding="utf-8"))
        if isinstance(record, dict) and record.get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError) as exc:
        log_debug(f"{STAMP_FILE_NAME} not cleared: {exc}")


def _stamp_problem(state_dir: Optional[Path]) -> Optional[str]:
    try:
        record = jsonsafe.loads(_stamp_path(state_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "it wrote no version stamp"
    except (OSError, ValueError):
        return "its version stamp is unreadable"
    current = code_version()
    if not isinstance(record, dict) or record.get("version") != current:
        version = record.get("version") if isinstance(record, dict) else None
        return f"its version stamp is {version!r}, this code is {current!r}"
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) \
            or not is_process_instance_alive(pid, record.get("start_time")):
        return "its version stamp names a process that is gone"
    return None


def outdated_holder(state_dir: Optional[Path] = None) -> Optional[str]:
    """Why the process holding ``reconciler.lock`` is not a reconciler of this code; None when it is (or none runs).

    A reconciler that took the lock an instant ago may not have stamped yet, so a problem is re-checked once
    after ``STAMP_SETTLE_SECONDS``.
    """
    lock = _stamp_path(state_dir).with_name(RECONCILER_LOCK_NAME)
    for attempt in range(2):
        if not lock.exists() or not reconciler_running(state_dir):   # never create the lock file just to probe it
            return None
        problem = _stamp_problem(state_dir)
        if problem is None:
            return None
        if attempt == 0:
            clock.sleep(STAMP_SETTLE_SECONDS)
    return problem


def outdated_warning(reason: str) -> str:
    return (f"An older herdr-bartender reconciler holds reconciler.lock ({reason}); the current reconciler code "
            f"cannot run until it exits. Stop it with: {STOP_COMMAND}")
