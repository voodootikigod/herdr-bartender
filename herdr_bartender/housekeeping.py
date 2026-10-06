"""Startup sweep of stale temporary files (Plan §6.2 step 4, §5.1 guard stdin sweep).

A tmp file older than 60s cannot belong to a live writer (every writer is
bounded far below that), so it is a leftover from a crash or a failed write.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from . import clock
from .log import log_debug
from .paths import get_orphan_path

STALE_TMP_SECONDS = 60


def _patterns(state_dir: Path, orphan_path: Path) -> Iterable[Tuple[Path, str]]:
    yield state_dir, "active-sessions.json.tmp.*"
    yield state_dir, "active-sessions.json.salvage.*"
    yield state_dir, ".guard_stdin.*"
    yield state_dir / "spool", "*.tmp"
    yield state_dir / "results", "*.tmp"
    yield state_dir / "panes", "*.vendor_active.claim-*"   # a retirement interrupted by a crash
    yield orphan_path.parent, f"{orphan_path.name}.tmp.*"
    yield orphan_path.parent / f"{orphan_path.name}.pending", ".*.tmp"


def _remove_if_stale(path: Path, now: float) -> bool:
    try:
        if now - path.stat().st_mtime <= STALE_TMP_SECONDS:
            return False
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        log_debug(f"Stale tmp sweep could not remove {path}: {e}")
        return False


def sweep_stale_temp_files(state_dir: Path, now: Optional[float] = None,
                           orphan_path: Optional[Path] = None) -> List[Path]:
    """Unlink cache/spool/results/orphan tmp files and guard stdin captures older than 60s."""
    now = clock.time() if now is None else now
    orphan_path = orphan_path or get_orphan_path()
    removed: List[Path] = []
    for directory, pattern in _patterns(state_dir, orphan_path):
        if not directory.is_dir():
            continue
        try:
            candidates = list(directory.glob(pattern))
        except OSError as e:
            log_debug(f"Stale tmp sweep could not list {directory}: {e}")
            continue
        removed.extend(p for p in candidates if _remove_if_stale(p, now))
    return removed
