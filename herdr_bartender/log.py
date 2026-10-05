"""Debug log with 1MB rotation into the state dir; the log files are always 0600 (Plan §8 L1089)."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

from . import clock
from .paths import PRIVATE_FILE_MODE, get_state_dir

LOG_FILE_NAME = "plugin.log"
ROTATED_LOG_FILE_NAME = "plugin.log.1"
LOG_MAX_BYTES = 1_048_576


def _rotate_if_needed(log_file: Path) -> None:
    if log_file.exists() and log_file.stat().st_size > LOG_MAX_BYTES:
        log_file.replace(log_file.with_name(ROTATED_LOG_FILE_NAME))


def _open_private_append(log_file: Path) -> int:
    fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
                 PRIVATE_FILE_MODE)
    if not stat.S_ISREG(os.fstat(fd).st_mode):   # R70: never write the log into a FIFO or device
        os.close(fd)
        raise OSError(f"{log_file.name} is not a regular file")
    if os.fstat(fd).st_mode & 0o077:
        os.fchmod(fd, PRIVATE_FILE_MODE)
    return fd


def log_debug(msg: str) -> None:
    try:
        log_file = get_state_dir() / LOG_FILE_NAME
        _rotate_if_needed(log_file)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(clock.time()))
        fd = _open_private_append(log_file)
        try:
            f = os.fdopen(fd, "a", encoding="utf-8")
        except Exception:
            os.close(fd)
            raise
        with f:
            f.write(f"[{stamp}] {msg}\n")
    except Exception:
        # Logging must never break the watchdog-bounded hook path, and there is
        # nowhere else to report a failure to write the log itself.
        pass


def log_warning(msg: str) -> None:
    """A log_debug line tagged `WARNING:` for fail-closed/degraded paths worth an operator's attention."""
    log_debug(f"WARNING: {msg}")
