"""Observation helpers for the mock bridge's ``probe`` hook."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Callable

from herdr_bartender import runtime


def cache_lock_probe(lock_file: Path) -> Callable[[], dict]:
    """A probe recording, at the moment a request reaches the bridge, whether the client process was inside
    a cache critical section and whether the cache lock file could be taken (LOCK_NB, released at once)."""

    def probe() -> dict:
        in_section = runtime.IN_CRITICAL_SECTION
        fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            free = True
        except BlockingIOError:
            free = False
        finally:
            os.close(fd)
        return {"in_critical_section": in_section, "cache_lock_free": free}

    return probe
