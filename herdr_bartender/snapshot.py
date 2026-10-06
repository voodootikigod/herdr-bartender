"""Process facts the reconciler needs, resolved BEFORE it takes the cache lock (gap lock-discipline-subprocess).

One ``ProcessSnapshot`` per reconciler pass: the current Bartender and Herdr instances
(earliest-start selection, ``process.probe_*``), Herdr liveness, and whether the
instances the cache last recorded (``last_bartender_pid`` / ``last_herdr_pid``, read
with a lock-free peek) are gone. Under the cache lock the lifecycle code only reads
this snapshot, so no ``pgrep``/``ps`` ever runs inside the critical section.

R14: an unknown start time or a failed probe is "no information" - it never counts
as a restart, and an unknown Herdr probe counts as alive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Tuple

from .process import (
    get_process_start_time,
    herdr_liveness,
    is_process_instance_alive,
    probe_bartender,
    probe_herdr,
)
from .sender.lease import peek_cache

BARTENDER = "bartender"
HERDR = "herdr"


@dataclass(frozen=True)
class Instance:
    """One probed process: ``known`` is False when the probe itself failed."""

    pid: Optional[int] = None
    start_time: Optional[str] = None
    known: bool = False

    @property
    def absent(self) -> bool:
        """Confirmed not running (the probe worked and found nothing)."""
        return self.known and self.pid is None


@dataclass(frozen=True)
class ProcessSnapshot:
    bartender: Instance = Instance()
    herdr: Instance = Instance()
    herdr_alive: bool = True
    # kind -> (pid recorded in the cache when the snapshot was taken, that instance is gone: True/False/None)
    previous: Mapping[str, Tuple[Optional[int], Optional[bool]]] = field(default_factory=dict)

    def instance(self, kind: str) -> Instance:
        return self.bartender if kind == BARTENDER else self.herdr

    def old_gone(self, kind: str, last_pid: object) -> Optional[bool]:
        """Whether the cached ``last_<kind>_pid`` instance is gone; None when unknown or the cache moved on."""
        pid, gone = self.previous.get(kind, (None, None))
        return gone if pid is not None and pid == last_pid else None


def _instance(pid: Optional[int], known: bool) -> Instance:
    return Instance(pid, get_process_start_time(pid) if pid is not None else None, known)


def _previous(peek: Mapping, kind: str) -> Tuple[Optional[int], Optional[bool]]:
    pid = peek.get(f"last_{kind}_pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None, None
    alive = is_process_instance_alive(pid, peek.get(f"last_{kind}_start_time"))
    return pid, not alive


def take_snapshot(state_dir: Path) -> ProcessSnapshot:
    """Probe Bartender, Herdr and the previously recorded instances (may spawn ``pgrep``/``ps``; never locked)."""
    peek = peek_cache(state_dir)
    bartender, herdr = probe_bartender(), probe_herdr()
    return ProcessSnapshot(
        bartender=_instance(bartender.pid, bartender.known),
        herdr=_instance(herdr.pid, herdr.known),
        herdr_alive=herdr_liveness() is not False,
        previous={BARTENDER: _previous(peek, BARTENDER), HERDR: _previous(peek, HERDR)},
    )
