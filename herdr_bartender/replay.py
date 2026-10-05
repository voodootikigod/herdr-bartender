"""Orphan replay: ``--replay-orphans <file>`` and the reconciler's automatic replay (Plan §9.2, §5.1 item 4; R10).

1. **Snapshot under the orphan lock** (bounded blocking wait, R10): parse the file (one that
   cannot be understood raises with the file and its journal untouched), fold the contention
   journal - so journaled exports are replayed even when the file itself does not exist yet -
   then take at most the 256 newest records (the rest stay for the next run). The lock is
   released before any network I/O (gap orphan-replay-lock-and-skip), so event-path exports
   never wait behind a POST.
2. **Validation**: a session id that fails ``SESSION_ID_REGEX`` can never be replayed; it
   is dropped from the file.
3. **Normative skip guard** against one read of the session cache: a record is skipped
   (and popped from the file) iff ``active = sessions.get(sid)`` (or the session on the
   same pane) is not None, not salvaged and not Ended. A salvaged session never suppresses
   a replay.
4. **Send** ``{"state": "Ended", "agent": <agent or "Herdr">, "session_id": sid}`` (0.2s),
   retried once with the minimal payload when unconfirmed (``bridge.deliver_event``; there
   is no second POST when the payload already is the minimal one, i.e. agent "Herdr").
5. **Post-send re-sync** (§9.2 4a) under the cache lock: a live session admitted for the
   session id/pane while the Ended was in flight is forced to re-sync (``delivered_seq = 0``,
   ``in_flight``, ``resync_generation`` bumped) and the reconciler is flagged; a salvaged one
   is never flipped (gap replay-marker-resync). A cached Ended for the session id is
   confirmed through the shared apply-result (evicted). The pane marker is removed only when
   no live session remains on the pane.
6. **Commit under the orphan lock again**: fold any new journal entries, then drop only the
   records that were confirmed, skipped or invalid AND are unchanged since the snapshot
   (concurrent exports are kept). An empty file is unlinked; otherwise it is rewritten
   atomically with mode 0600. Unconfirmed records are retained with ``replay_attempts`` /
   ``last_replay_at`` (unless re-exported meanwhile: new data starts a new schedule).
7. **Backoff of the automatic replay** (W4: §5.1 item 11 idle exit): the reconciler replays a
   record only when it is due - never attempted, or ``last_replay_at`` + 20s doubling per
   attempt (capped at 300s) has passed. A record in backoff is not reconciler work: the loop
   may idle out and the next reconciler run replays it. ``--replay-orphans`` ignores the
   backoff.

Replay never removes ``$STATE_DIR`` and never touches ``DISABLED`` (§9.2 item 7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from . import clock, runtime
from .bridge import DEFAULT_EVENT_TIMEOUT, deliver_event
from .cache import BoundedSessionCache, CacheError
from .config import SESSION_ID_REGEX
from .delivery_state import (
    STATUS_SUCCESS,
    Outcome,
    StagedEffects,
    Transmission,
    apply_delivery_result,
    commit_delivery_down,
    force_resync_superseding_senders,
)
from .handoff import ensure_reconciler_running, touch_reconciler_pending
from .log import log_debug, log_warning
from .markers import remove_pane_marker
from .orphans import (
    OrphanFileError,
    acquire_orphan_lock,
    fold_journal_locked,
    journal_waiting,
    pending_dir_for,
    read_orphan_records,
    release_orphan_lock,
    write_orphan_sessions,
)
from .paths import get_state_dir

REPLAY_CAPACITY = 256
REPLAY_SOCKET_SECONDS = DEFAULT_EVENT_TIMEOUT   # Plan §9.2 item 3: 0.2s
REPLAY_BACKOFF_BASE_SECONDS = 20.0              # one reconciler cadence
REPLAY_BACKOFF_MAX_SECONDS = 300.0              # the absence backoff interval
ATTEMPTS_FIELD, LAST_ATTEMPT_FIELD = "replay_attempts", "last_replay_at"

Between = Optional[Callable[[], None]]   # called between records (the reconciler's marker heartbeat)


@dataclass(frozen=True)
class ReplayReport:
    confirmed: Tuple[str, ...] = ()
    skipped: Tuple[str, ...] = ()
    invalid: Tuple[str, ...] = ()
    unconfirmed: Tuple[str, ...] = ()
    beyond_capacity: int = 0
    remaining: Optional[int] = None   # records left in the file after the commit (None: commit failed)
    resynced: Tuple[str, ...] = field(default=())
    deferred: int = 0                 # records in backoff, not replayed by an automatic run

    @property
    def ok(self) -> bool:
        return not self.unconfirmed and not self.beyond_capacity and self.remaining is not None


# -- automatic replay backoff (pure) ------------------------------------------------------------
def _count(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0


def _stamp(value: object) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def replay_backoff(attempts: int) -> float:
    """Seconds after unconfirmed attempt ``attempts`` (>= 1) before the next automatic one: 20s doubling, max 300s."""
    return min(REPLAY_BACKOFF_MAX_SECONDS, REPLAY_BACKOFF_BASE_SECONDS * 2 ** max(0, attempts - 1))


def replay_due(record: object, now: float) -> bool:
    """The reconciler may replay ``record`` now (never attempted, backoff over, or the wall clock stepped back)."""
    if not isinstance(record, dict):
        return True   # an invalid record is dropped by its next replay
    attempts, last = _count(record.get(ATTEMPTS_FIELD)), _stamp(record.get(LAST_ATTEMPT_FIELD))
    if attempts == 0 or last is None or last > now:
        return True
    return now >= last + replay_backoff(attempts)


def with_attempt(record: object, now: float) -> dict:
    """``record`` with one more unconfirmed attempt stamped (a non-dict record carries nothing replay uses: it
    becomes a dict, so it backs off like any other)."""
    base = record if isinstance(record, dict) else {}
    return {**base, ATTEMPTS_FIELD: _count(base.get(ATTEMPTS_FIELD)) + 1, LAST_ATTEMPT_FIELD: now}


def auto_replay_due(path: Path, now: float, include_journal: bool = True) -> bool:
    """Reconciler work: journaled operations to fold (unless ``include_journal`` is False), or a record whose
    automatic replay is due.

    Lock-free read (the file is only ever replaced atomically); an unreadable file is not work the
    automatic replay could do (it is left untouched for ``--replay-orphans`` and the operator).
    """
    if include_journal and journal_waiting(path):
        return True
    try:
        records = read_orphan_records(path)
    except OrphanFileError as exc:
        log_debug(f"Orphan file not replayable automatically: {exc}")
        return False
    return any(replay_due(record, now) for record in records.values())


# -- file access (under the orphan lock) ------------------------------------------------------
_read_records = read_orphan_records


def _locked(path: Path):
    fd = acquire_orphan_lock(path, blocking=True)
    if fd is None:
        raise OrphanFileError(f"orphan lock for {path} still contended")
    return fd


def snapshot_records(path: Path) -> Dict[str, object]:
    """Parse the file, fold the journal, then read the records (orphan lock held only for this local I/O).

    The file is parsed BEFORE the fold: a file that cannot be understood raises OrphanFileError with the file
    and its journal untouched (the fold itself refuses to rewrite it, too).
    """
    fd = _locked(path)
    try:
        _read_records(path)
        fold_journal_locked(path)
        return _read_records(path)
    finally:
        release_orphan_lock(fd)


def _after_commit(current: Mapping[str, object], settled: Mapping[str, object],
                  retried: Mapping[str, Tuple[object, object]]) -> Dict[str, object]:
    """Pure: drop settled records and stamp retried ones, both only when unchanged since the snapshot."""
    remaining: Dict[str, object] = {}
    for sid, rec in current.items():
        if sid in settled and rec == settled[sid]:
            continue
        snapshot, stamped = retried.get(sid, (None, None))
        remaining[sid] = stamped if sid in retried and rec == snapshot else rec
    return remaining


def commit_settled(path: Path, settled: Mapping[str, object],
                   retried: Optional[Mapping[str, Tuple[object, object]]] = None) -> int:
    """Drop the settled records, stamp the unconfirmed ones (each only if unchanged); returns how many remain."""
    fd = _locked(path)
    try:
        fold_journal_locked(path)
        current = _read_records(path)
        remaining = _after_commit(current, settled, retried or {})
        if not remaining:
            path.unlink(missing_ok=True)
        else:  # §9.2 item 6: always the atomic 0600 rewrite (also normalises a hand-written file's mode/shape)
            write_orphan_sessions(path, remaining)
        return len(remaining)
    finally:
        release_orphan_lock(fd)


# -- the skip guard and the post-send re-sync (cache lock) ----------------------------------------------
def record_pane(sid: str, record: object) -> Optional[str]:
    pane = record.get("pane_id") if isinstance(record, dict) else None
    if isinstance(pane, str) and pane:
        return pane
    parts = sid.split(":")
    return ":".join(parts[2:]) if len(parts) >= 3 else None


def find_active(sessions: Mapping, sid: str, pane: Optional[str]) -> Optional[dict]:
    active = sessions.get(sid)
    if active is None and pane:
        active = next((r for r in sessions.values() if isinstance(r, dict) and r.get("pane_id") == pane), None)
    return active if isinstance(active, dict) else None


def suppresses_replay(active: Optional[Mapping]) -> bool:
    """Plan §9.2 item 3: the single normative skip predicate (salvaged sessions never suppress)."""
    return active is not None and not active.get("salvaged", False) and active.get("desired_state") != "Ended"


def _live_on_pane(sessions: Mapping, pane: Optional[str]) -> bool:
    return bool(pane) and any(isinstance(r, dict) and r.get("pane_id") == pane and r.get("desired_state") != "Ended"
                              and not r.get("salvaged") for r in sessions.values())


def _confirm_cached_ended(data: dict, sid: str) -> Optional[StagedEffects]:
    """A cached Ended for ``sid`` is confirmed through the shared apply-result (None: nothing cached to confirm)."""
    cached = data.get("sessions", {}).get(sid)
    if not isinstance(cached, dict) or cached.get("desired_state") != "Ended":
        return None
    return apply_delivery_result(data, Transmission.snapshot(sid, cached, None), Outcome(STATUS_SUCCESS))


def settle_confirmed(cache_mgr: BoundedSessionCache, sid: str, pane: Optional[str]) -> bool:
    """After a confirmed orphan Ended: re-sync a live re-admission, confirm a cached Ended; True when the
    reconciler must run (a re-sync)."""
    with cache_mgr as data:
        sessions = data.get("sessions", {})
        active = find_active(sessions, sid, pane)
        resync = suppresses_replay(active)
        if resync:
            log_debug(f"Live session for {sid} admitted while its orphan Ended was in flight; forcing re-sync")
            force_resync_superseding_senders(active)
        confirmed = _confirm_cached_ended(data, sid)
        if confirmed is not None or resync:
            cache_mgr.save(data)
        if confirmed is not None:
            commit_delivery_down(confirmed)   # a reconnection clears DELIVERY_DOWN only once its re-sync is saved
        if pane and not _live_on_pane(data.get("sessions", {}), pane):
            remove_pane_marker(pane)
    # touch_pending: e.g. the confirmation was the reconnection after DELIVERY_DOWN
    return resync or (confirmed is not None and confirmed.touch_pending)


# -- replay ---------------------------------------------------------------------------------------
def _send_ended(sid: str, record: object, bridge_url: Optional[str]) -> bool:
    agent = record.get("agent") if isinstance(record, dict) else None
    payload = {"state": "Ended", "agent": agent if isinstance(agent, str) and agent else "Herdr", "session_id": sid}
    return deliver_event(payload, timeout=REPLAY_SOCKET_SECONDS, bridge_url=bridge_url).success


def _cache_sessions(cache_mgr: BoundedSessionCache) -> dict:
    with cache_mgr as data:
        return dict(data.get("sessions", {}))


@dataclass
class _Run:
    """Mutable accumulator for one replay run (local to ``replay_records``)."""

    settled: Dict[str, object] = field(default_factory=dict)
    retried: Dict[str, Tuple[object, object]] = field(default_factory=dict)
    confirmed: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    invalid: List[str] = field(default_factory=list)
    unconfirmed: List[str] = field(default_factory=list)
    resynced: List[str] = field(default_factory=list)


def _settle_unsent(run: _Run, sid: object, record: object, sessions: dict, emit) -> bool:
    """Invalid id or skip guard: settled without a POST (True), else False."""
    if not isinstance(sid, str) or not SESSION_ID_REGEX.match(sid):
        emit(f"    [-] Skipping orphan with invalid session_id: {str(sid)[:64]!r}")
        run.invalid.append(sid)
        run.settled[sid] = record
        return True
    active = find_active(sessions, sid, record_pane(sid, record))
    if suppresses_replay(active):
        emit(f"    [*] Skipping orphan {sid}: pane currently has active non-Ended session {active.get('desired_state')}")
        run.skipped.append(sid)
        run.settled[sid] = record
        return True
    return False


def _replay_one(run: _Run, sid: str, record: object, sessions: dict, cache_mgr: BoundedSessionCache,
                bridge_url: Optional[str], emit) -> None:
    if _settle_unsent(run, sid, record, sessions, emit):
        return
    if not _send_ended(sid, record, bridge_url):
        emit(f"    [-] Failed to deliver Ended for {sid}")
        run.unconfirmed.append(sid)
        run.retried[sid] = (record, with_attempt(record, clock.time()))
        return
    emit(f"    [+] Cleared {sid}")
    run.confirmed.append(sid)
    run.settled[sid] = record
    try:
        if settle_confirmed(cache_mgr, sid, record_pane(sid, record)):
            run.resynced.append(sid)
    except CacheError as exc:  # the Ended landed; a missed re-sync is caught by the next reconciler pass
        log_warning(f"Post-send re-sync check for {sid} skipped ({exc}); flagging the reconciler")
        run.resynced.append(sid)


def replay_records(records: Mapping[str, object], bridge_url: Optional[str], emit, between: Between = None) -> _Run:
    cache_mgr = BoundedSessionCache(get_state_dir())   # DISABLED is not consulted: replay works after rollback
    sessions = _cache_sessions(cache_mgr)
    run = _Run()
    for sid, record in records.items():
        _replay_one(run, sid, record, sessions, cache_mgr, bridge_url, emit)
        if between is not None:
            between()
    if run.resynced:
        touch_reconciler_pending()
        ensure_reconciler_running()
    return run


def _emitter(quiet: bool):
    def emit(message: str) -> None:
        log_debug(f"orphan replay: {message.strip()}")
        if not quiet:
            print(message)
    return emit


def replay_orphan_file(path: Path, bridge_url: Optional[str] = None, quiet: bool = False, only_due: bool = False,
                       between: Between = None) -> Optional[ReplayReport]:
    """One replay run over ``path``; None when there is no orphan file (nor journal) to replay.

    ``only_due`` (the reconciler's automatic replay) leaves records in backoff alone. Raises OrphanFileError when
    the file cannot be locked or understood.
    """
    emit = _emitter(quiet)
    records = snapshot_records(path)
    if not records and not path.exists():
        return None
    now = clock.time()
    candidates = {sid: r for sid, r in records.items() if not only_due or replay_due(r, now)}
    batch = dict(list(candidates.items())[-REPLAY_CAPACITY:])   # the newest exports (the file appends them)
    deferred = len(records) - len(candidates)
    if not batch:
        return ReplayReport(remaining=len(records), deferred=deferred)
    emit(f"[*] Replaying {len(batch)} orphaned session(s) from {path}...")
    run = replay_records(batch, bridge_url, emit, between)
    remaining = commit_settled(path, run.settled, run.retried)
    if remaining == 0:
        emit(f"[+] Successfully cleared all orphaned sessions; removed {path}")
    return ReplayReport(tuple(run.confirmed), tuple(run.skipped), tuple(run.invalid), tuple(run.unconfirmed),
                        max(0, len(candidates) - len(batch)), remaining, tuple(run.resynced), deferred)


def run_replay_orphans(orphan_path_str: str, bridge_url: Optional[str] = None, quiet: bool = False,
                       only_due: bool = False, between: Between = None) -> bool:
    """``--replay-orphans <file>``: True when every replayed record was confirmed or skipped (Plan §9.2)."""
    path = Path(orphan_path_str).expanduser()
    report = None
    if path.exists() or pending_dir_for(path).is_dir():
        with runtime.deadline_mode(runtime.DEADLINE_UNBOUNDED):  # Plan L619/L686: exempt from the 1.5s watchdog
            try:
                report = replay_orphan_file(path, bridge_url=bridge_url, quiet=quiet, only_due=only_due,
                                            between=between)
            except (OrphanFileError, CacheError, OSError) as exc:
                _emitter(quiet)(f"[-] Orphan replay failed: {exc}")
                return False
    if report is None:
        _emitter(quiet)(f"[-] Orphan file not found: {path}")
        return False
    return report.ok
