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


class OversizedFile(ValueError):
    """The file is larger than the caller's bound."""


class DirectoryFull(OSError):
    """The capped directory is at its ceiling (or its lock could not be taken in time)."""


def read_regular_file(path: Path, max_bytes: int) -> bytes:
    """At most ``max_bytes`` of a regular file; raises OSError (incl. not-regular / symlink) or OversizedFile."""
    return read_regular_file_stat(path, max_bytes)[0]


def read_regular_file_stat(path: Path, max_bytes: int) -> Tuple[bytes, os.stat_result]:
    """``read_regular_file`` plus the opened file's ``fstat`` (its identity, for compare-and-retire)."""
    fd = os.open(str(path), _READ_FLAGS)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
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
        raise OversizedFile(f"{path.name} is larger than {max_bytes} bytes")
    return data, st


def capped_lock_timeout() -> float:
    """Lock wait for a capped write: the event-path budget rule (Plan §4.3), unbounded paths get the 0.2s cap."""
    from . import runtime   # late: runtime imports half the package
    return min(0.2, max(0.02, runtime.time_remaining() - 0.3))


def _count_json(directory: Path) -> int:
    return sum(1 for p in directory.glob("*.json") if p.is_file())


@contextmanager
def capped_directory(directory: Path, cap: int, timeout: float = 0.2) -> Iterator[None]:
    """Hold ``<directory>/.lock`` while the body writes one file; raises DirectoryFull at the ceiling."""
    fd = os.open(str(directory / LOCK_NAME), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                 PRIVATE_FILE_MODE)
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
        count = _count_json(directory)
        if count >= cap:
            raise DirectoryFull(errno.ENOSPC, f"{count} files at the {cap}-file ceiling", str(directory))
        yield
    finally:
        os.close(fd)   # releases the flock
