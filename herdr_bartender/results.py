"""Delivery result envelopes (Plan §4.3 Step C lock failure) and their drain (§5.1 item 1a).

When a sender cannot re-acquire the cache lock in Step C (or cannot save there),
``defer_result()`` writes ``results/<timestamp_ns>_<pid>_<seq>.json`` so the
confirmation is not lost. ``drain_results_dir()`` (the reconciler, before spool
replay) applies them FIFO under one lock hold with the same
``delivery_state.apply_delivery_result()`` Step C uses, saves once, unlinks only
the files whose effects were saved, then runs staged orphan I/O outside the lock.
A backlog larger than one batch keeps ``reconciler.pending`` set so the rest is
drained on the next pass instead of waiting for an unrelated wake-up.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from . import clock
from .cache import BoundedSessionCache
from .delivery_state import (
    RESULT_STATUSES,
    Outcome,
    StagedEffects,
    Transmission,
    apply_delivery_result,
    commit_delivery_down,
    empty_effects,
    run_orphan_effects,
)
from .envelopes import quarantine, read_json, unlink_files, write_json_atomic
from .log import log_debug, log_warning
from .paths import ensure_private_dir, get_state_dir
from .handoff import ensure_reconciler_running, touch_reconciler_pending

RESULTS_VERSION = 1
RESULTS_BATCH = 32


class ResultWriteError(Exception):
    """The result envelope could not be written."""


@dataclass(frozen=True)
class DrainReport:
    applied: int
    quarantined: int
    effects: StagedEffects


def results_dir(state_dir: Optional[Path] = None) -> Path:
    return ensure_private_dir((state_dir or get_state_dir()) / "results")


def build_result_envelope(tx: Transmission, outcome: Outcome, timestamp_ns: int, pid: int) -> dict:
    """Plan fields (version, session_id, transmitting_seq/state, status, error, timestamp_ns, pid) plus the
    rest of the Transmission snapshot needed to apply it exactly like Step C."""
    return {
        "version": RESULTS_VERSION,
        "session_id": tx.session_id,
        "transmitting_seq": tx.seq,
        "transmitting_state": tx.state,
        "status": outcome.status,
        "error": outcome.error,
        "timestamp_ns": timestamp_ns,
        "pid": pid,
        "pane_id": tx.pane_id,
        "agent": tx.agent,
        "lease_token": tx.lease_token,
        "resync_generation": tx.resync_generation,
        "generation": tx.generation,
        "admitted_at_ns": tx.admitted_at_ns,
        "arrival_ns": tx.arrival_ns,
    }


def _unique_path(directory: Path, timestamp_ns: int, pid: int, seq: int) -> Tuple[Path, int]:
    while (directory / f"{timestamp_ns:020d}_{pid}_{seq}.json").exists():
        timestamp_ns += 1
    return directory / f"{timestamp_ns:020d}_{pid}_{seq}.json", timestamp_ns


def write_result_envelope(tx: Transmission, outcome: Outcome, state_dir: Optional[Path] = None) -> Path:
    """Atomically write one result envelope; raises ResultWriteError."""
    try:
        directory = results_dir(state_dir)
        pid = os.getpid()
        path, stamp = _unique_path(directory, clock.time_ns(), pid, tx.seq)
        write_json_atomic(path, build_result_envelope(tx, outcome, stamp, pid))
        return path
    except (OSError, TypeError, ValueError) as e:
        raise ResultWriteError(f"could not write result for {tx.session_id} seq {tx.seq}: {e}") from e


def defer_result(tx: Transmission, outcome: Outcome, reason: object) -> Optional[Path]:
    """Step C could not lock or save: persist the outcome for the reconciler and hand off (never raises)."""
    path = None
    try:
        path = write_result_envelope(tx, outcome)
        log_debug(f"Step C deferred for {tx.session_id} seq {tx.seq} ({reason}); wrote {path.name}")
    except ResultWriteError as e:
        log_warning(f"Step C result for {tx.session_id} seq {tx.seq} lost ({reason}): {e}")
    touch_reconciler_pending()
    ensure_reconciler_running()
    return path


def parse_result_envelope(env: object) -> Tuple[Transmission, Outcome]:
    """Validate a decoded envelope; raises ValueError naming the problem."""
    if not isinstance(env, dict) or env.get("version") != RESULTS_VERSION:
        raise ValueError("not a version-1 result envelope")
    if env.get("status") not in RESULT_STATUSES:
        raise ValueError(f"unknown status {str(env.get('status'))[:32]!r}")
    sid, seq, state = env.get("session_id"), env.get("transmitting_seq"), env.get("transmitting_state")
    if not isinstance(sid, str) or not sid or not isinstance(state, str):
        raise ValueError("session_id/transmitting_state missing")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise ValueError("transmitting_seq must be a non-negative integer")
    raw = {k: env.get(k) for k in ("pane_id", "agent", "lease_token", "generation", "admitted_at_ns", "arrival_ns")}
    tx = Transmission(sid, raw["pane_id"], state, seq, raw["agent"] or "Herdr", raw["lease_token"],
                      int(env.get("resync_generation") or 0), raw["generation"], raw["admitted_at_ns"],
                      raw["arrival_ns"])
    return tx, Outcome(env["status"], env.get("error"))


@dataclass(frozen=True)
class _Batch:
    entries: Tuple[Tuple[Path, Transmission, Outcome], ...]
    quarantined: int
    backlog: bool   # more envelopes than one batch were waiting


def _load_batch(directory: Path, max_batch: int) -> _Batch:
    entries, quarantined = [], 0
    files = sorted(p for p in directory.glob("*.json") if p.is_file())
    for path in files[:max_batch]:
        try:
            tx, outcome = parse_result_envelope(read_json(path))
        except (OSError, ValueError) as e:
            quarantined += int(quarantine(path, directory / "bad", f"invalid result envelope: {e}"))
            continue
        entries.append((path, tx, outcome))
    return _Batch(tuple(entries), quarantined, len(files) > max_batch)


def drain_results_dir(state_dir: Path, max_batch: int = RESULTS_BATCH,
                      cache_mgr: Optional[BoundedSessionCache] = None) -> DrainReport:
    """Apply up to ``max_batch`` result envelopes FIFO under one lock hold (raises CacheError on lock/save failure)."""
    directory = Path(state_dir) / "results"
    if not directory.is_dir():
        return DrainReport(0, 0, empty_effects())
    batch = _load_batch(directory, max_batch)
    if not batch.entries:
        if batch.backlog:
            touch_reconciler_pending()
        return DrainReport(0, batch.quarantined, empty_effects())
    cache_mgr = cache_mgr or BoundedSessionCache(state_dir)
    merged = empty_effects()
    with cache_mgr as data:
        for _, tx, outcome in batch.entries:   # one critical section: later results see earlier DELIVERY_DOWN changes
            merged = merged.merge(apply_delivery_result(data, tx, outcome, delivery_down=merged.delivery_down))
        cache_mgr.save(data)
        commit_delivery_down(merged)   # only once saved: an unsaved reconnection keeps DELIVERY_DOWN for the retry
        unlink_files([path for path, _, _ in batch.entries])  # under the lock: no other drainer can re-apply them
    run_orphan_effects(merged)
    if merged.touch_pending or batch.backlog:  # backlog: drain the rest on the next pass, not after an idle sleep
        touch_reconciler_pending()
    return DrainReport(len(batch.entries), batch.quarantined, merged)
