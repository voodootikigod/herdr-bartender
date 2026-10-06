"""Hold a flock from a separate child process (true multi-process contention).

``hold_lock(testcase, path)`` starts a child Python process that opens ``path``,
takes ``LOCK_EX`` and blocks until it is killed. It returns only once the child
reports that it holds the lock, and registers a cleanup that kills the child.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

_CHILD = r"""
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
sys.stdout.write("locked\n")
sys.stdout.flush()
time.sleep(float(sys.argv[2]))
"""


class LockHolder:
    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc

    def release(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        if self.proc.stdout is not None:
            self.proc.stdout.close()


def hold_lock(testcase: unittest.TestCase, path: Path, seconds: float = 30.0) -> LockHolder:
    """Start a child that holds ``LOCK_EX`` on ``path``; returns once the lock is held."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(path), str(seconds)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    holder = LockHolder(proc)
    testcase.addCleanup(holder.release)
    line = proc.stdout.readline() if proc.stdout is not None else ""
    if line.strip() != "locked":
        holder.release()
        raise RuntimeError(f"lock holder failed to lock {path}")
    return holder
