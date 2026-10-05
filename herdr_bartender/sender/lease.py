"""Lease takeover truth table (Plan §1 L71-79), lease claims, and the pre-lock liveness warm-up for Step A.

Plan §6.1 keeps the Step A critical section CPU-bounded: no subprocess may run while
the cache lock is held. The liveness facts Step A can consult (a lease holder's start
time, Herdr liveness for the tombstone gate) are therefore resolved by
``warm_step_a_probes()`` BEFORE locking, and read under the lock through the 0.5s
memo only (``process.memoised_*``). Anything not memoised is unknown and handled
conservatively: an unknown holder start time never rejects a live holder (R14), an
unknown Herdr probe counts as alive (as a failed probe does).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from ..boundedio import CACHE_MAX_BYTES, read_regular_file
from .. import jsonsafe, runtime
from ..cache import CACHE_FILE_NAME
from ..delivery_state import LEASE_GRACE_SECONDS, LEASE_SECONDS, Transmission
from ..delivery_state import foreign_lease_open as _foreign_lease_open
from ..log import log_debug
from ..process import (
    get_process_start_time,
    is_herdr_alive,
    is_pid_alive,
    known_start_time,
    memoised_instance_alive,
    own_start_time,
    warm_process_identity,
)

# Plan §1 L68: one lease duration (1.5s) for every code path - the Step A claim reuses delivery_state's constant.
LEASE_FIELDS = ("lease_token", "sending_pid", "lease_deadline")

InstanceAlive = Callable[[Optional[int], Optional[str]], bool]


@dataclass(frozen=True)
class Claim:
    """A lease this process holds on one session: the transmission snapshot taken under the lock (Step A)."""

    session_id: str
    pane_id: Optional[str]
    payload: dict
    state: str
    seq: int
    token: str
    resync_gen: int
    generation: Optional[int]
    admitted_at_ns: Optional[int]
    arrival_ns: Optional[int]

    @property
    def agent(self) -> str:
        return self.payload.get("agent") or "Herdr"

    def transmission(self) -> Transmission:
        return Transmission(self.session_id, self.pane_id, self.state, self.seq, self.agent, self.token,
                            self.resync_gen, self.generation, self.admitted_at_ns, self.arrival_ns)


def new_lease_token(session_id: str, now_wall: float) -> str:
    """Plan §1 L66: ``{pid}:{PROCESS_START_TIME}:{now}:{sid}`` (start time warmed before the lock)."""
    return f"{os.getpid()}:{own_start_time()}:{now_wall}:{session_id}"


def claim_lease(session_id: str, pane_id: Optional[str], record: dict, now_wall: float,
                arrival_ns: Optional[int]) -> Claim:
    """Claim ``record`` for this process (under the cache lock) and snapshot what Step B transmits."""
    token = new_lease_token(session_id, now_wall)
    resync_gen = int(record.get("resync_generation", 0) or 0)
    record.update({"lease_token": token, "sending_pid": os.getpid(), "lease_deadline": now_wall + LEASE_SECONDS,
                   "lease_resync_gen": resync_gen})
    payload = dict(record.get("desired_payload") or {})
    state = record.get("desired_state") or payload.get("state") or ""
    return Claim(session_id, pane_id or record.get("pane_id"), payload, state, int(record.get("seq", 0) or 0),
                 token, resync_gen, record.get("generation"), record.get("admitted_at_ns"), arrival_ns)


def release_lease(record: dict) -> None:
    record.update({field: None for field in LEASE_FIELDS})


def owns_lease(record: Optional[dict], token: str) -> bool:
    return bool(record) and record.get("lease_token") == token


def _holder_start_time(record: dict) -> Optional[str]:
    parts = str(record.get("lease_token") or "").split(":")
    return parts[1] if len(parts) >= 2 else None


def lease_active_elsewhere(record: dict, now_wall: float,
                           instance_alive: InstanceAlive = memoised_instance_alive) -> bool:
    """Rows 4/5: another live sender (PID + start time) within deadline + 0.5s grace -> DEFER.

    Called under the cache lock, so the default ``instance_alive`` never spawns ``ps``.
    """
    if not _foreign_lease_open(record, now_wall):
        return False
    return instance_alive(record.get("sending_pid"), _holder_start_time(record))


# -- pre-lock warm-up --------------------------------------------------------------------
def peek_cache(state_dir: Path) -> dict:
    """Lock-free snapshot of the (atomically replaced) cache; {} when absent or unreadable.

    It only decides which probes to warm before locking; no state decision is ever
    taken from it.
    """
    try:
        data = jsonsafe.loads(read_regular_file(Path(state_dir) / CACHE_FILE_NAME, CACHE_MAX_BYTES))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log_debug(f"Pre-lock cache peek skipped: {e}")
        return {}
    return data if isinstance(data, dict) else {}


def _records(snapshot: dict) -> Iterable[dict]:
    sessions = snapshot.get("sessions")
    if not isinstance(sessions, dict):
        return ()
    return (rec for rec in sessions.values() if isinstance(rec, dict))


def _spool_pending(state_dir: Path) -> bool:
    try:
        return any((Path(state_dir) / "spool").glob("*.json"))
    except OSError:
        return False


def warm_step_a_probes(state_dir: Path, now_wall: float) -> None:
    """Resolve, before the cache lock, the liveness lookups Step A may need (each memoised for 0.5s).

    * this process's own start time (the lease token, Plan §1 L66);
    * the start time of every live foreign lease holder whose token carries a known start time;
    * Herdr liveness when a tombstone exists or spooled envelopes await replay (tombstone gate).

    Stops early once the deadline has passed (the caller only saves or spools then).
    """
    warm_process_identity()
    snapshot = peek_cache(state_dir)
    for record in _records(snapshot):
        if runtime.PENDING_WATCHDOG_EXIT:
            return
        pid = _holder_needing_start_time(record, now_wall)
        if pid is not None:
            get_process_start_time(pid)
    if not runtime.PENDING_WATCHDOG_EXIT and (snapshot.get("tombstones") or _spool_pending(state_dir)):
        is_herdr_alive()


def _holder_needing_start_time(record: dict, now_wall: float) -> Optional[int]:
    """The live foreign lease holder whose start time the lease check will compare, else None."""
    pid = record.get("sending_pid")
    if not isinstance(pid, int) or not _foreign_lease_open(record, now_wall):
        return None
    if known_start_time(_holder_start_time(record)) is None or not is_pid_alive(pid):
        return None
    return pid
