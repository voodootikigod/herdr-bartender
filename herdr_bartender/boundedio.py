"""Bounded, non-blocking file I/O for state files read or written on the critical path (R64).

* ``read_regular_file``: opens without following symlinks and without blocking (a FIFO or device planted in the
  state dir can never stall a reader), refuses anything that is not a regular file, and reads at most
  ``max_bytes`` - a larger file raises instead of being loaded whole.
* ``capped_directory``: a short exclusive lock on ``<dir>/.lock`` so "count the files, then write one" is atomic
  across processes; the cap is a strict ceiling, not a best effort.
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Tuple

from . import clock
from .paths import PRIVATE_FILE_MODE

LOCK_NAME = ".lock"
LOCK_RETRY_INTERVAL = 0.005
_READ_FLAGS = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)


# R65: per-file read bounds for every state file read on the plugin's paths.
CACHE_MAX_BYTES = 16 * 1024 * 1024        # 256 sessions with their payloads fit with a wide margin
ORPHAN_FILE_MAX_BYTES = 8 * 1024 * 1024   # at most 256 exported records
JOURNAL_ENTRY_MAX_BYTES = 256 * 1024      # one orphan op (one record)
SMALL_STATE_MAX_BYTES = 64 * 1024         # SHA allowlist, reconciler stamp, HOOK_NEEDS_REVIEW
HOOK_SCRIPT_MAX_BYTES = 1024 * 1024       # vendor hook scripts are a few KiB


class UnusableFile(OSError, ValueError):
    """Not a usable state file (too large, or not a regular file); both an OSError and a ValueError, so every
    existing ``except OSError`` / ``except ValueError`` handler treats it as unreadable / corrupt."""


class OversizedFile(UnusableFile):
    """The file is larger than the caller's bound."""


class NotRegularFile(UnusableFile):
    """A FIFO, device, directory or symlink where a regular state file was expected."""


class DirectoryFull(OSError):
    """The capped directory is at its ceiling (or its lock could not be taken in time)."""


def read_regular_file(path: Path, max_bytes: int, follow_symlinks: bool = False) -> bytes:
    """At most ``max_bytes`` of a regular file; raises OSError (incl. not-regular / symlink) or OversizedFile.

    ``follow_symlinks`` is only for files outside the state dir that users may legitimately symlink (vendor hooks).
    """
    return read_regular_file_stat(path, max_bytes, follow_symlinks)[0]


def read_regular_file_stat(path: Path, max_bytes: int,
                           follow_symlinks: bool = False) -> Tuple[bytes, os.stat_result]:
    """``read_regular_file`` plus the opened file's ``fstat`` (its identity, for compare-and-retire)."""
    flags = _READ_FLAGS if not follow_symlinks else os.O_RDONLY | os.O_NONBLOCK
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:   # O_NOFOLLOW refused a symlink
            raise NotRegularFile(errno.ELOOP, "is a symlink", str(path)) from exc
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise NotRegularFile(errno.EINVAL, "not a regular file", str(path))
        chunks, remaining = [], max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > max_bytes:
        raise OversizedFile(errno.EFBIG, f"larger than {max_bytes} bytes", str(path))
    return data, st


def read_regular_prefix(path: Path, max_bytes: int) -> bytes:
    """The first ``max_bytes`` of a regular file (never raises OversizedFile): salvage of an oversized cache."""
    try:
        return read_regular_file(path, max_bytes)
    except OversizedFile:
        fd = os.open(str(path), _READ_FLAGS)
        try:
            chunks, remaining = [], max_bytes
            while remaining > 0:
                chunk = os.read(fd, min(remaining, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NONBLOCK | _NOFOLLOW


def _require_regular(fd: int, path: Path) -> None:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise NotRegularFile(errno.EINVAL, "not a regular file", str(path))


def _open_no_follow(path: Path, flags: int, mode: int) -> int:
    try:
        return os.open(str(path), flags, mode)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise NotRegularFile(errno.ELOOP, "is a symlink", str(path)) from exc
        raise


def write_state_file(path: Path, data: bytes = b"") -> None:
    """R70: write (or touch, with ``b""``) a small state file: never follows a symlink, never blocks on a FIFO
    (no reader: ENXIO), and refuses anything that is not a regular file; created 0600."""
    fd = _open_no_follow(path, _WRITE_FLAGS, PRIVATE_FILE_MODE)
    try:
        _require_regular(fd, path)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)


def open_exclusive_tmp(path: Path, mode: int = PRIVATE_FILE_MODE) -> int:
    """R70: create a predictable temp file with O_EXCL (a stale or planted path - symlink included - is unlinked,
    never followed, then created afresh); returns a write fd."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW
    try:
        return os.open(str(path), flags, mode)
    except FileExistsError:
        os.unlink(str(path))   # removes a symlink itself, never its target
        return os.open(str(path), flags, mode)


def open_lock_file(path: Path) -> int:
    """R70: open (creating) a lock/append target without following symlinks or blocking on a FIFO."""
    fd = _open_no_follow(path, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | _NOFOLLOW, PRIVATE_FILE_MODE)
    try:
        _require_regular(fd, path)
    except BaseException:
        os.close(fd)
        raise
    return fd


def capped_lock_timeout() -> float:
    """Lock wait for a capped write: the event-path budget rule (Plan §4.3), unbounded paths get the 0.2s cap."""
    from . import runtime   # late: runtime imports half the package
    return min(0.2, max(0.02, runtime.time_remaining() - 0.3))


def _count_json(directory: Path) -> int:
    return sum(1 for p in directory.glob("*.json") if p.is_file())


@contextmanager
def locked_directory(directory: Path, timeout: float = 0.2) -> Iterator[None]:
    """Hold ``<directory>/.lock`` (exclusive, bounded wait); raises DirectoryFull(EAGAIN) when it stays busy."""
    fd = open_lock_file(directory / LOCK_NAME)
    try:
        give_up_at = clock.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if clock.monotonic() >= give_up_at:
                    raise DirectoryFull(errno.EAGAIN, "directory lock busy", str(directory))
                clock.sleep(LOCK_RETRY_INTERVAL)
        yield
    finally:
        os.close(fd)   # releases the flock


@contextmanager
def capped_directory(directory: Path, cap: int, timeout: float = 0.2) -> Iterator[None]:
    """Hold ``<directory>/.lock`` while the body writes one file; raises DirectoryFull at the ceiling."""
    with locked_directory(directory, timeout):
        count = _count_json(directory)
        if count >= cap:
            raise DirectoryFull(errno.ENOSPC, f"{count} files at the {cap}-file ceiling", str(directory))
        yield
