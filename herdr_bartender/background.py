"""--reconcile-background: the singleton reconciler loop (Plan §5.1 item 2; R10, R13, R14, R21, R26).

The loop holds ``reconciler.lock`` (a second runner touches ``reconciler.pending`` and
exits) and runs in ``runtime.deadline_mode(DEADLINE_UNBOUNDED)``: it never inherits the
1.4/1.5s event deadline (gaps bg-deadline-clamp, background-budget-starvation). Each
full pass (``_sweep_pass``):

1. one process snapshot (no subprocess under the cache lock) and a marker heartbeat;
2. results drain (shared apply-result), spool replay, orphan-journal flush;
3. health probe when anything waits on the bridge; a healthy bridge clears DELIVERY_DOWN,
   re-arms exhausted sessions and - after DELIVERY_DOWN - forces a Full Re-Sync (a Step C
   that confirms first does the same: ``delivery_state``);
4. automatic orphan replay of the records that are due (per-record backoff) when ``/health``
   is ok;
5. ``reconciler.reconcile_active_sessions`` (compensations, vendor cleanups, lifecycle,
   R12 horizon, per-session Universal Sender sweep, dismissals);
6. a second journal flush (exports journaled by this pass), the 20s marker heartbeat
   (outside the lock), the stale tmp/``.guard_stdin.*`` sweep and the non-approving hook
   integrity repair.

Inside a long pass the heartbeat also runs whenever 20s passed (between replayed records and
between sends), so markers stay under 60s old (item 5). Between passes the loop sleeps in
0.5s ticks until the next due time (``schedule``), waking at once when ``reconciler.pending``
is touched or a spool/results envelope arrives (``wake``). A pending touch re-runs a pass at
once, but a second immediate re-run in a row waits one 0.5s tick first, so no self-inflicted
touch can make it a busy loop; the reconciler's own orphan-journal entries are no wake-up at
all (``handoff.reconciler_loop``).
"""

from __future__ import annotations

import fcntl
import os
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import clock, runtime
from .bridge import check_bridge_health
from .cache import BoundedSessionCache, CacheError, IntegrationDisabled
from .delivery_state import rearm_exhausted
from .handoff import (
    PENDING_FILE_NAME,
    RECONCILER_LOCK_NAME,
    ensure_reconciler_running,
    reconciler_loop,
    touch_reconciler_pending,
)
from .hooks_integrity import repair_hooks_if_allowlisted
from .hooks_install import NO_HOOKS
from .housekeeping import sweep_stale_temp_files
from .lifecycle import all_sessions, full_resync
from .log import log_debug, log_warning
from .markers import clear_delivery_down, is_delivery_down, is_disabled, refresh_pane_marker
from .orphans import flush_pending_orphan_ops, journal_waiting
from .paths import get_orphan_path, get_state_dir
from .reconciler import evict_exported_sessions, reconcile_active_sessions
from .reconciler_stamp import clear_stamp, code_version, write_stamp
from .replay import auto_replay_due, run_replay_orphans
from .results import drain_results_dir
from .schedule import (
    HEARTBEAT_SECONDS,
    CacheView,
    LoopState,
    after_pass,
    cache_view,
    cancel_backoff,
    full_pass_due,
    idle_expired,
    next_wake,
    terminal_horizon_reached,
    track_idle,
    track_presence,
)
from .sender import BACKGROUND_POLICY
from .snapshot import ProcessSnapshot, take_snapshot
from .spool import replay_spool_dir
from .wake import WAKE_TICK_SECONDS, Envelopes, consume_pending, envelope_files, envelopes_waiting, \
    sleep_until, still_stuck

# A pass that hit a CacheError (lock timeout, unreadable/unwritable cache) is retried after a growing
# sleep, never immediately, and the run gives up after CACHE_FAILURE_LIMIT consecutive failures
# (reconciler.pending stays set, so the next spawn retries).
CACHE_RETRY_BASE_SECONDS = 0.5
CACHE_RETRY_MAX_SECONDS = 8.0
CACHE_FAILURE_LIMIT = 6
TERMINAL_EXPORT_ATTEMPTS = 3   # the loop exits after the terminal export: retry what could not be written first


@dataclass(frozen=True)
class PassOutcome:
    snapshot: ProcessSnapshot
    view: CacheView
    bridge_healthy: Optional[bool] = None   # None: not probed this pass
    journal_flushed: bool = True            # False: the orphan journal could not be folded (not idle-blocking)


def cache_retry_delay(failures: int) -> float:
    """Seconds to wait before re-running a pass after ``failures`` consecutive CacheErrors (>= 1)."""
    return min(CACHE_RETRY_MAX_SECONDS, CACHE_RETRY_BASE_SECONDS * (2 ** max(0, failures - 1)))


def _retry_after_cache_error(failures: int) -> bool:
    """Back off after a failed pass. True: re-run the pass now; False: give up this run."""
    touch_reconciler_pending()
    if failures >= CACHE_FAILURE_LIMIT:
        log_warning(f"Reconciler giving up after {failures} consecutive cache failures; reconciler.pending kept")
        return False
    delay = cache_retry_delay(failures)
    log_debug(f"Reconciler pass will be retried in {delay:.1f}s")
    deadline = clock.monotonic() + delay
    while clock.monotonic() < deadline and not is_disabled():
        clock.sleep(min(CACHE_RETRY_BASE_SECONDS, deadline - clock.monotonic()))
    return True


# -- heartbeat ------------------------------------------------------------------------------
def _read_view(cache_mgr: BoundedSessionCache, snap: ProcessSnapshot) -> CacheView:
    with cache_mgr as data:
        return cache_view(data, clock.time(), snap.herdr_alive)


def _heartbeat(view: CacheView, snap: ProcessSnapshot) -> int:
    """Plan §5.1 item 5 / R13: refresh delivered live panes' existing markers while Herdr is alive (outside the lock)."""
    if not snap.herdr_alive:
        return 0
    return sum(1 for pane in dict.fromkeys(view.heartbeat_panes) if refresh_pane_marker(pane))


class _PassHeartbeat:
    """The heartbeat inside one full pass: at its start, then whenever 20s passed (between records/sends)."""

    def __init__(self, cache_mgr: BoundedSessionCache, snap: ProcessSnapshot) -> None:
        self._cache_mgr, self._snap = cache_mgr, snap
        self._next = clock.monotonic()

    def __call__(self) -> None:
        if clock.monotonic() < self._next:
            return
        self._next = clock.monotonic() + HEARTBEAT_SECONDS
        try:
            _heartbeat(_read_view(self._cache_mgr, self._snap), self._snap)
        except (CacheError, IntegrationDisabled) as exc:
            log_debug(f"In-pass heartbeat skipped: {exc!r}")


# -- one pass ---------------------------------------------------------------------------
def _probe_health(bridge_url: Optional[str]) -> bool:
    health = check_bridge_health(bridge_url=bridge_url)
    return bool(health) and health.get("ok") is True


def _health_needed(view: CacheView, orphans_waiting: bool) -> bool:
    """Probe /health when exhausted sessions, DELIVERY_DOWN or orphans wait on it, or to confirm idleness."""
    return view.exhausted > 0 or is_delivery_down() or orphans_waiting or view.sessions == 0


def _recover(cache_mgr: BoundedSessionCache) -> None:
    """Healthy bridge (Plan §5.1 items 3-4): clear DELIVERY_DOWN, re-arm exhausted sessions; a reconnection
    after DELIVERY_DOWN is a Full Top Shelf Re-Sync (gap delivery-down-resync-missing)."""
    with cache_mgr as data:
        was_down = is_delivery_down()   # under the lock: a concurrent Step C may have re-synced and cleared it
        data["consecutive_failures"] = 0
        rearmed = rearm_exhausted(data)
        resynced = full_resync(data) if was_down else ()
        if rearmed or resynced or was_down:
            cache_mgr.save(data)
        clear_delivery_down()   # only once the re-sync is saved
    if was_down or rearmed:
        log_debug(f"Bridge healthy: re-armed {rearmed} exhausted session(s), re-synced {len(resynced)}")


def _flush_orphan_journal(orphan_path: Path) -> bool:
    """R10: fold orphan exports/removals journaled under event-path lock contention into the orphan file."""
    if flush_pending_orphan_ops(orphan_file=orphan_path, blocking=True):
        return True
    log_warning(f"Orphan journal for {orphan_path} not flushed this pass; it stays queued")
    return False


def _check_hooks(state_dir: Path) -> None:
    """Plan §5.1 item 10: non-approving repair, gated on DISABLED/NO_HOOKS (never ``install_hooks``)."""
    if is_disabled() or (state_dir / NO_HOOKS).exists():
        return
    try:
        repair_hooks_if_allowlisted(state_dir)
    except Exception as exc:  # a broken hook check must never stop the reconciler
        log_warning(f"Hook integrity check failed: {exc!r}")


def _bridge_phase(state_dir: Path, cache_mgr: BoundedSessionCache, bridge_url: Optional[str],
                  snap: ProcessSnapshot, beat: _PassHeartbeat) -> Optional[bool]:
    """Steps 1a/1b, journal flush, health recovery and orphan replay; the health verdict (None: not probed)."""
    drain_results_dir(state_dir)
    replay_spool_dir(state_dir)
    orphan_path = get_orphan_path()
    _flush_orphan_journal(orphan_path)
    orphans_waiting = auto_replay_due(orphan_path, clock.time())
    if not _health_needed(_read_view(cache_mgr, snap), orphans_waiting):
        return None
    healthy = _probe_health(bridge_url)
    if healthy:
        _recover(cache_mgr)
        if orphans_waiting:
            log_debug("Bridge healthy and orphans due: automatic orphan replay")
            run_replay_orphans(str(orphan_path), bridge_url=bridge_url, quiet=True, only_due=True, between=beat)
    return healthy


def _sweep_pass(state_dir: Path, cache_mgr: BoundedSessionCache, bridge_url: Optional[str]) -> PassOutcome:
    """One full pass (raises CacheError for the backoff; IntegrationDisabled ends the loop)."""
    snap = take_snapshot(state_dir)
    beat = _PassHeartbeat(cache_mgr, snap)
    beat()
    healthy = _bridge_phase(state_dir, cache_mgr, bridge_url, snap, beat)
    reconcile_active_sessions(state_dir, bridge_url=bridge_url, snapshot=snap, between=beat)
    flushed = _flush_orphan_journal(get_orphan_path())   # exports journaled during this pass reach the file now
    view = _read_view(cache_mgr, snap)
    _heartbeat(view, snap)
    sweep_stale_temp_files(state_dir)
    _check_hooks(state_dir)
    return PassOutcome(snap, view, healthy, flushed)


def _heartbeat_pass(state_dir: Path, cache_mgr: BoundedSessionCache) -> PassOutcome:
    """A wake during the 300s backoff: presence tracking and the 20s heartbeat only (R13)."""
    snap = take_snapshot(state_dir)
    view = _read_view(cache_mgr, snap)
    _heartbeat(view, snap)
    return PassOutcome(snap, view, None)


# -- terminal horizon ----------------------------------------------------------------------
def _cached_sessions(cache_mgr: BoundedSessionCache):
    with cache_mgr as data:
        return all_sessions(data)


def _export_undelivered(cache_mgr: BoundedSessionCache) -> None:
    """Bartender absent >12h and Herdr dead: export every cached session to the orphan file and evict it.

    The reconciler exits right after (and nothing respawns it while Herdr is dead), so the exports wait for
    the orphan lock (R10 blocking mode, bounded) instead of being journaled, and a session whose export could
    not be written is retried - after a journal flush - up to TERMINAL_EXPORT_ATTEMPTS times; whatever is
    still left is logged and flagged in ``reconciler.pending`` for the next run.
    """
    try:
        for attempt in range(TERMINAL_EXPORT_ATTEMPTS):
            exports = _cached_sessions(cache_mgr)
            if not exports:
                return
            if attempt:
                _flush_orphan_journal(get_orphan_path())
            evict_exported_sessions(cache_mgr, exports, "terminal absence horizon")
        left = [sid for sid, _ in _cached_sessions(cache_mgr)]
    except (CacheError, IntegrationDisabled) as exc:
        log_warning(f"Could not export undelivered sessions ({exc}); leaving them for the next reconciler run")
        touch_reconciler_pending()
        return
    if left:
        log_warning(f"Terminal horizon: {len(left)} session(s) could not be exported and stay cached: {left}")
        touch_reconciler_pending()


# -- the loop --------------------------------------------------------------------------------
def _orphan_work(outcome: PassOutcome) -> bool:
    """Due orphan records; journaled operations too, unless this pass could not fold them (retried next run)."""
    path = get_orphan_path()
    if outcome.journal_flushed and journal_waiting(path):
        return True
    return auto_replay_due(path, clock.time(), include_journal=False)


def _work_waiting(state_dir: Path, outcome: PassOutcome, stuck: Envelopes = frozenset()) -> bool:
    view = outcome.view
    return bool(view.sessions or view.owed or envelopes_waiting(state_dir, stuck)
                or _orphan_work(outcome) or (state_dir / PENDING_FILE_NAME).exists())


def _bridge_settled(outcome: PassOutcome) -> bool:
    """A healthy bridge, or (R40) Bartender confirmed not running: with nothing cached, owed or queued there is
    nothing to deliver or re-sync when it starts, and any new event starts a reconciler again."""
    return outcome.bridge_healthy is True or outcome.snapshot.bartender.absent


def _idle_now(state_dir: Path, outcome: PassOutcome, stuck: Envelopes = frozenset()) -> bool:
    """Plan §5.1 item 11 (R40): 0 sessions, nothing owed or queued, and a healthy (or absent) bridge."""
    return _bridge_settled(outcome) and not is_delivery_down() and not _work_waiting(state_dir, outcome, stuck)


def _cache_has_work(cache_mgr: BoundedSessionCache) -> bool:
    """A session or owed side effect in the cache right now (re-read: the pass's view may be stale).

    An event that admits and delivers a session needs no hand-off (its ``ensure_watchdog()`` only probes
    ``reconciler.lock``), so a reconciler that is idling out must look at the cache itself. An unreadable
    cache counts as work: a needless successor idles out again, a stranded session would lose its heartbeat.
    """
    try:
        with cache_mgr as data:
            view = cache_view(data, clock.time(), True)
    except IntegrationDisabled:
        return False
    except CacheError as exc:
        log_warning(f"Idle-exit cache check failed ({exc}); assuming work is waiting")
        return True
    return bool(view.sessions or view.owed)


@dataclass(frozen=True)
class _Step:
    outcome: PassOutcome
    full: bool


def _run_step(state_dir: Path, cache_mgr: BoundedSessionCache, bridge_url: Optional[str], state: LoopState,
              force_full: bool) -> _Step:
    now = clock.time()
    full = force_full or full_pass_due(state, now)
    outcome = _sweep_pass(state_dir, cache_mgr, bridge_url) if full else _heartbeat_pass(state_dir, cache_mgr)
    return _Step(outcome, full)


def _advance(state_dir: Path, state: LoopState, step: _Step, stuck: Envelopes) -> LoopState:
    now = clock.time()
    outcome = step.outcome
    state = track_presence(state, outcome.snapshot.bartender, now)
    if step.full:
        state = track_idle(state, _idle_now(state_dir, outcome, stuck), now)
    return after_pass(state, now, step.full, outcome.view.next_due)


def _pause(state_dir: Path, state: LoopState, stuck: Envelopes, reruns: int) -> int:
    """Sleep until the next wake-up; returns the count of consecutive immediate re-runs.

    New work flagged during the pass (``reconciler.pending``) re-runs the pass at once (Plan §5.1 item 3), but a
    second re-run in a row first waits one 0.5s tick: whoever keeps touching it, the loop never spins.
    """
    if (state_dir / PENDING_FILE_NAME).exists():
        if reruns:
            clock.sleep(WAKE_TICK_SECONDS)
        return reruns + 1
    sleep_until(next_wake(state, clock.time()), state_dir, PENDING_FILE_NAME, stuck)
    return 0


@dataclass
class _LoopRun:
    """Mutable bookkeeping of one ``_run_loop`` call."""

    state: LoopState = LoopState()
    stuck: Envelopes = frozenset()
    failures: int = 0
    reruns: int = 0


def _one_iteration(state_dir: Path, cache_mgr: BoundedSessionCache, bridge_url: Optional[str],
                   run: _LoopRun, loop_once: bool) -> Optional[str]:
    """One pass and its bookkeeping; returns "stop", "idle", "retry" or None (continue sleeping)."""
    pending_file = state_dir / PENDING_FILE_NAME
    # Plan §5.1 item 3: a pending touch or a spool/results arrival forces a full pass and cancels the backoff
    if consume_pending(pending_file) or envelopes_waiting(state_dir, run.stuck):
        run.state = cancel_backoff(run.state)
        forced = True
    else:
        forced = loop_once
    before = envelope_files(state_dir)
    try:
        step = _run_step(state_dir, cache_mgr, bridge_url, run.state, forced)
        run.failures = 0
    except IntegrationDisabled:
        return "stop"
    except CacheError as exc:
        run.failures += 1
        log_warning(f"Reconciler pass incomplete ({exc}); failure {run.failures} of {CACHE_FAILURE_LIMIT}")
        return "retry" if _retry_after_cache_error(run.failures) else "stop"
    if loop_once:
        return "stop"
    run.stuck = still_stuck(before, envelope_files(state_dir), run.stuck, step.full)
    run.state = _advance(state_dir, run.state, step, run.stuck)
    now = clock.time()
    if terminal_horizon_reached(run.state, step.outcome.snapshot.herdr, now):
        log_warning("Bartender absent >12h and Herdr dead: exporting every session to the orphan file and exiting")
        _export_undelivered(cache_mgr)
        return "stop"
    if idle_expired(run.state, now):
        if not _cache_has_work(cache_mgr):
            log_debug("Idle for 60s with no sessions and a healthy bridge; reconciler exiting")
            return "idle"
        log_debug("A session was admitted after this pass read the cache; not idle")
        run.state = track_idle(run.state, False, now)
    run.reruns = _pause(state_dir, run.state, run.stuck, run.reruns)
    return None


def _run_loop(state_dir: Path, bridge_url: Optional[str], loop_once: bool) -> bool:
    """The reconciler loop; True when it ended by idling out (the caller then checks for lost wake-ups)."""
    cache_mgr = BACKGROUND_POLICY.cache(state_dir)
    run = _LoopRun()
    while not is_disabled():
        verdict = _one_iteration(state_dir, cache_mgr, bridge_url, run, loop_once)
        if verdict == "idle":
            return True
        if verdict == "stop":
            break
    return False


def _start_successor_if_needed(state_dir: Path) -> None:
    """Lost wake-up guard for an idle exit, called once ``reconciler.lock`` is released.

    An event that ran after the loop's final check (while the lock was still held) found the
    singleton busy and spawned nothing: one that flagged ``reconciler.pending``, and one that
    admitted and delivered a session (its ``ensure_watchdog()`` only probes the lock). Hand
    that work to a successor now instead of stranding it until the next event. Re-reading the
    cache AFTER the release closes the window: an event whose lock probe came before the
    release saved its session before that probe.
    """
    if is_disabled():
        return
    if (state_dir / PENDING_FILE_NAME).exists():
        log_debug("reconciler.pending was set while the reconciler exited; starting a successor")
    elif _cache_has_work(BACKGROUND_POLICY.cache(state_dir)):
        log_debug("A session was admitted while the reconciler exited; starting a successor")
    else:
        return
    ensure_reconciler_running()


def _acquire_singleton(state_dir: Path) -> Optional[int]:
    """``reconciler.lock`` (LOCK_NB); None when another reconciler holds it (``reconciler.pending`` touched)."""
    lock_fd = None
    try:
        lock_fd = os.open(str(state_dir / RECONCILER_LOCK_NAME), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_fd
    except OSError as exc:
        if not isinstance(exc, BlockingIOError):
            log_warning(f"Could not take {RECONCILER_LOCK_NAME}: {exc}")
        touch_reconciler_pending(state_dir)
        if lock_fd is not None:
            os.close(lock_fd)
        return None


def _release_singleton(lock_fd: int) -> None:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError as exc:
        log_debug(f"Could not unlock {RECONCILER_LOCK_NAME} (closing releases it): {exc}")
    finally:
        os.close(lock_fd)


def run_reconcile_background(bridge_url: Optional[str] = None, loop_once: bool = False) -> None:
    if is_disabled():
        return
    state_dir = get_state_dir()
    code_version()   # R38: digest the code this process loaded before an upgrade can change it on disk
    lock_fd = _acquire_singleton(state_dir)
    if lock_fd is None:
        return
    idle_exit = False
    try:
        with runtime.deadline_mode(runtime.DEADLINE_UNBOUNDED), reconciler_loop():
            write_stamp(state_dir)   # R38: who holds reconciler.lock (an upgrade can leave an older holder)
            idle_exit = _run_loop(state_dir, bridge_url, loop_once)
    except Exception as exc:  # recorded for the operator; reconciler.pending keeps the work for the next spawn
        log_warning(f"Reconciler crashed: {exc!r}\n{traceback.format_exc()}")
        touch_reconciler_pending(state_dir)
        raise
    finally:
        clear_stamp(state_dir)
        _release_singleton(lock_fd)
    if idle_exit:
        with runtime.deadline_mode(runtime.DEADLINE_UNBOUNDED):
            _start_successor_if_needed(state_dir)
