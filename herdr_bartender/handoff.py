"""Reconciler hand-off: ``reconciler.pending`` and the singleton-aware detached spawner (Plan §5.1).

* ``touch_reconciler_pending()`` flags work for the reconciler loop (it re-runs a pass
  instead of sleeping).
* ``ensure_reconciler_running()`` starts ``bin/herdr-bartender --reconcile-background``
  detached (own session, no inherited stdio) unless one already holds
  ``reconciler.lock`` (the singleton) or the integration is DISABLED.

Process creation goes through an injectable ``Spawner`` (``set_spawner``). Production
uses ``DetachedSpawner``; the test sandbox installs a recording spawner, so no code
path here branches on a test-only environment variable.

This module is a leaf (it imports only ``log`` and ``paths``) so the watchdog, the
cache, the spool and the results drain can all hand work off without import cycles.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from .log import log_debug
from .paths import PRIVATE_FILE_MODE, get_state_dir, launcher_path

PENDING_FILE_NAME = "reconciler.pending"
RECONCILER_LOCK_NAME = "reconciler.lock"
RECONCILE_FLAG = "--reconcile-background"


class Spawner:
    """Starts a detached process; returns True when it was started."""

    def spawn(self, argv: Sequence[str]) -> bool:  # pragma: no cover - interface
        raise NotImplementedError


class DetachedSpawner(Spawner):
    """Production spawner: ``Popen`` in a new session with no inherited stdio or descriptors."""

    def spawn(self, argv: Sequence[str]) -> bool:
        try:
            subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            log_debug(f"Failed to spawn detached process {list(argv)[-2:]}: {exc}")
            return False
        return True


_SPAWNER: Spawner = DetachedSpawner()


def get_spawner() -> Spawner:
    return _SPAWNER


def set_spawner(spawner: Spawner) -> Spawner:
    """Install ``spawner`` process-wide; returns the previous one (so callers can restore it)."""
    global _SPAWNER
    previous, _SPAWNER = _SPAWNER, spawner
    return previous


def reconciler_argv(*extra: str) -> List[str]:
    return [sys.executable, str(launcher_path()), RECONCILE_FLAG, *extra]


def _state_dir(state_dir: Optional[Path]) -> Path:
    return Path(state_dir) if state_dir is not None else get_state_dir()


def touch_reconciler_pending(state_dir: Optional[Path] = None) -> None:
    try:
        (_state_dir(state_dir) / PENDING_FILE_NAME).touch(exist_ok=True)
    except OSError as exc:
        log_debug(f"Could not touch {PENDING_FILE_NAME}: {exc}")


def reconciler_running(state_dir: Optional[Path] = None) -> bool:
    """True while another process holds ``reconciler.lock`` (probed with LOCK_NB, released at once)."""
    lock_path = _state_dir(state_dir) / RECONCILER_LOCK_NAME
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, PRIVATE_FILE_MODE)
    except OSError as exc:
        log_debug(f"Reconciler lock probe failed ({exc}); assuming no reconciler runs")
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError as exc:
        log_debug(f"Reconciler lock probe failed ({exc}); assuming no reconciler runs")
        return False
    finally:
        os.close(fd)  # closing the descriptor also drops a lock this probe took
    return False


def ensure_reconciler_running(state_dir: Optional[Path] = None) -> bool:
    """Start the detached reconciler unless one runs (singleton) or DISABLED exists. True if spawned."""
    directory = _state_dir(state_dir)
    if (directory / "DISABLED").exists():
        return False
    if reconciler_running(directory):
        return False
    return get_spawner().spawn(reconciler_argv())


def hand_off_to_reconciler(state_dir: Optional[Path] = None) -> None:
    """Flag the reconciler (a re-run pass) and make sure one is running."""
    touch_reconciler_pending(state_dir)
    ensure_reconciler_running(state_dir)
