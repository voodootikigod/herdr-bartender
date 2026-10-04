"""--reconcile-background singleton loop."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess

from . import clock
from .bridge import check_bridge_health
from .cache import BoundedSessionCache, CacheError
from .housekeeping import sweep_stale_temp_files
from .hooks import get_vendor_hooks_dir, install_hooks, verify_vendor_hooks_intact
from .log import log_debug, log_warning
from .markers import clear_delivery_down, is_delivery_down, is_disabled, touch_pane_marker
from .orphans import export_orphan_record, flush_pending_orphan_ops, run_replay_orphans
from .paths import get_orphan_path, get_state_dir
from .process import get_bartender_pid, is_herdr_alive
from .reconciler import reconcile_active_sessions
from .sender.compensation import next_compensation_wait
from .results import drain_results_dir
from .handoff import ensure_reconciler_running, touch_reconciler_pending
from .spool import replay_spool_dir

# A pass that hit a CacheError (lock timeout, unreadable/unwritable cache) is retried after a growing
# sleep, never immediately, and the run gives up after CACHE_FAILURE_LIMIT consecutive failures
# (reconciler.pending stays set, so the next spawn retries).
CACHE_RETRY_BASE_SECONDS = 0.5
CACHE_RETRY_MAX_SECONDS = 8.0
CACHE_FAILURE_LIMIT = 6
# An owed compensating Ended keeps the loop alive; it wakes when the next one is due on its retry schedule
# (sender.compensation), never sooner than the sleep loop's poll interval.
COMPENSATION_WAKE_FLOOR_SECONDS = 0.5


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


def _compensation_wait(cache_mgr) -> float | None:
    """Seconds until the earliest owed compensating Ended is due; None when none is owed (or the cache is unreadable,
    in which case the pass itself reports the CacheError)."""
    try:
        with cache_mgr as data:
            entries = list(data.get("pending_compensations") or [])
    except CacheError as exc:
        log_debug(f"Owed compensations unreadable ({exc}); not counted this pass")
        return None
    return next_compensation_wait(entries, clock.time())


def _sleep_seconds(base: float, owed_wait: float | None) -> float:
    if owed_wait is None:
        return base
    return min(base, max(COMPENSATION_WAKE_FLOOR_SECONDS, owed_wait))


def _envelopes_waiting(state_dir, names=("spool", "results")) -> bool:
    """Spool events and/or deferred delivery results still to be applied."""
    return any((state_dir / name).is_dir() and any((state_dir / name).glob("*.json")) for name in names)


def _export_undelivered(cache_mgr) -> None:
    """Bartender absent >12h and Herdr dead: export every undelivered session to the orphan file.

    The reconciler exits right after, so the exports wait for the orphan lock (R10 blocking mode) instead of
    being journaled for a reconciler run that may never come.
    """
    try:
        with cache_mgr as data:
            orphans_to_export = {
                sid: dict(s) for sid, s in data.get("sessions", {}).items()
                if s.get("delivered_state") != "Ended"
            }
    except CacheError as exc:
        log_warning(f"Could not export undelivered sessions ({exc}); leaving them for the next reconciler run")
        touch_reconciler_pending()
        return
    for sid, s in orphans_to_export.items():
        export_orphan_record(sid, s, blocking=True)


def _start_successor_if_flagged(pending_file) -> None:
    """Lost wake-up guard for an idle exit, called once ``reconciler.lock`` is released.

    An event that flagged ``reconciler.pending`` after the loop's final check (while the
    lock was still held) found the singleton busy and spawned nothing; hand its work to a
    successor now instead of stranding it until the next event.
    """
    if pending_file.exists() and not is_disabled():
        log_debug("reconciler.pending was set while the reconciler exited; starting a successor")
        ensure_reconciler_running()


def run_reconcile_background(bridge_url: str | None = None, loop_once: bool = False):
    if is_disabled():
        return
    state_dir = get_state_dir()
    lock_file = state_dir / "reconciler.lock"
    pending_file = state_dir / "reconciler.pending"
    lock_fd = None
    try:
        lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        try:
            pending_file.touch(exist_ok=True)
        except Exception:
            pass
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except Exception:
                pass
        return

    idle_exit = False
    try:
        cache_mgr = BoundedSessionCache(state_dir)
        bartender_absent_since: float | None = None
        cache_failures = 0
        while not is_disabled():
            if pending_file.exists():
                try:
                    pending_file.unlink()
                except Exception:
                    pass

            exhausted_count = active_count = 0
            try:
                exhausted_count, active_count = _sweep_pass(state_dir, cache_mgr, bridge_url)
                cache_failures = 0
            except CacheError as exc:
                cache_failures += 1
                log_warning(f"Reconciler pass incomplete ({exc}); failure {cache_failures} of {CACHE_FAILURE_LIMIT}")

            # Automatic guard integrity check: verify and re-install if drift occurred (gated on SHA allowlist)
            if not is_disabled() and not (state_dir / "NO_HOOKS").exists():
                intact, missing = verify_vendor_hooks_intact()
                if not intact and missing:
                    import hashlib
                    hooks_dir = get_vendor_hooks_dir()
                    sha_file = state_dir / "vendor-hook-sha.json"
                    known_shas = {}
                    if sha_file.exists():
                        try:
                            with open(sha_file, "r", encoding="utf-8") as sf:
                                known_shas = json.load(sf)
                        except Exception:
                            known_shas = {}
                    can_autopatch = True
                    for m_name in missing:
                        m_file = hooks_dir / m_name
                        if m_file.exists():
                            try:
                                with open(m_file, "r", encoding="utf-8") as mf:
                                    m_content = mf.read()
                                if "# BEGIN HERDR-BARTENDER DEDUP GUARD" in m_content and "# END HERDR-BARTENDER DEDUP GUARD" in m_content:
                                    m_clean = m_content.split("# BEGIN HERDR-BARTENDER DEDUP GUARD")[0] + m_content.split("# END HERDR-BARTENDER DEDUP GUARD")[1]
                                else:
                                    m_clean = m_content
                                clean_sha = hashlib.sha256(m_clean.encode("utf-8")).hexdigest()
                                if known_shas and m_name in known_shas and clean_sha != known_shas[m_name]:
                                    can_autopatch = False
                                    log_debug(f"Upstream vendor hook {m_name} modified (SHA mismatch: {clean_sha} != {known_shas[m_name]}); creating HOOK_NEEDS_REVIEW")
                            except Exception:
                                can_autopatch = False
                    if can_autopatch:
                        install_hooks()
                    else:
                        hnr_file = state_dir / "HOOK_NEEDS_REVIEW"
                        alerted_file = state_dir / ".hook_review_alerted"
                        hnr_file.touch(exist_ok=True)
                        if not alerted_file.exists():
                            alerted_file.touch(exist_ok=True)
                            try:
                                subprocess.run([
                                    "osascript", "-e",
                                    'display notification "Vendor hook updated. Run \\"herdr-bartender --install-hooks\\" to re-enable dedup." with title "Herdr Bartender Bridge"'
                                ], check=False, timeout=2.0)
                            except Exception:
                                pass

            if cache_failures:
                if not _retry_after_cache_error(cache_failures):
                    break
                continue

            # If pending file was touched during the sweep pass, re-run loop immediately
            if pending_file.exists():
                bartender_absent_since = None
                continue

            has_spool = _envelopes_waiting(state_dir)
            owed_wait = _compensation_wait(cache_mgr)
            if loop_once or (not pending_file.exists() and not has_spool and active_count == 0 and exhausted_count == 0 and not is_delivery_down() and owed_wait is None):
                idle_exit = not loop_once
                break
            if loop_once:
                break

            # Bartender presence tracking and wall-clock absence backoff
            curr_b_pid = get_bartender_pid()
            if curr_b_pid is None:
                if bartender_absent_since is None:
                    bartender_absent_since = clock.time()
                elif (clock.time() - bartender_absent_since > 43200) and not is_herdr_alive():
                    log_debug("Bartender absent >12h and Herdr dead past terminal horizon; exporting undelivered to orphans and exiting")
                    _export_undelivered(cache_mgr)
                    break
            else:
                bartender_absent_since = None

            sleep_time = 300.0 if (bartender_absent_since is not None and clock.time() - bartender_absent_since > 1000) else 20.0
            sleep_time = _sleep_seconds(sleep_time, owed_wait)
            sleep_deadline = clock.time() + sleep_time
            while clock.time() < sleep_deadline and not is_disabled():
                # results/ writers touch reconciler.pending themselves; a leftover results file must not
                # turn this wake-up check into a busy loop.
                if pending_file.exists() or _envelopes_waiting(state_dir, ("spool",)):
                    bartender_absent_since = None
                    break
                clock.sleep(0.5)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        except Exception:
            pass
    if idle_exit:
        _start_successor_if_flagged(pending_file)


def _flush_orphan_journal(orphan_path) -> None:
    """R10: fold orphan exports/removals journaled under event-path lock contention into the orphan file.

    Blocking lock (the reconciler may wait for --replay-orphans / --cleanup); never under the cache lock.
    A failure is logged and retried on the next pass or by the next orphan-lock holder.
    """
    if not flush_pending_orphan_ops(orphan_file=orphan_path, blocking=True):
        log_warning(f"Orphan journal for {orphan_path} not flushed this pass; it stays queued")


def _sweep_pass(state_dir, cache_mgr, bridge_url):
    """Steps 1a/1b/2/4/5 of one pass; returns (exhausted_count, active_count). Raises CacheError."""
    drain_results_dir(state_dir)
    replay_spool_dir(state_dir)
    reconcile_active_sessions(state_dir, bridge_url=bridge_url)

    # Drain the R10 contention journal first, so a flushed export is replayed in this same pass
    orphan_path = get_orphan_path()
    _flush_orphan_journal(orphan_path)

    # Automatic orphan replay when bridge is healthy and orphan file exists
    if orphan_path.exists():
        health = check_bridge_health(bridge_url=bridge_url)
        if health and health.get("ok") is True:
            log_debug("Bridge healthy and orphan file exists: triggering automatic orphan replay")
            run_replay_orphans(str(orphan_path), bridge_url=bridge_url)

    # Active health recovery for quiet outages: probe bridge health if exhausted sessions or DELIVERY_DOWN
    exhausted_count = 0
    with cache_mgr as data:
        sessions = data.get("sessions", {})
        for s in sessions.values():
            if s.get("delivery_status") == "retryable_exhausted":
                exhausted_count += 1

    if exhausted_count > 0 or is_delivery_down():
        health = check_bridge_health(bridge_url=bridge_url)
        if health and health.get("ok") is True:
            clear_delivery_down()
            with cache_mgr as data:
                data["consecutive_failures"] = 0
                for s in data.get("sessions", {}).values():
                    if s.get("delivery_status") == "retryable_exhausted":
                        s["delivery_status"] = "in_flight"
                        s["delivery_attempts"] = 0
                        s.pop("delivery_error", None)
                cache_mgr.save(data)
            reconcile_active_sessions(state_dir, bridge_url=bridge_url)

    # Heartbeat: refresh mtime of delivered, non-salvaged, non-Ended pane markers (including Idle and Done) while Herdr is alive
    active_count = 0
    with cache_mgr as data:
        sessions = data.get("sessions", {})
        for sid, s in list(sessions.items()):
            if not s.get("salvaged") and s.get("delivered_state") not in (None, "Ended") and s.get("delivery_status") == "delivered":
                if is_herdr_alive():
                    touch_pane_marker(s.get("pane_id"))
                active_count += 1

    # Stale tmp files (cache/spool/results/orphan) and guard stdin captures older than 60s
    sweep_stale_temp_files(state_dir)
    return exhausted_count, active_count
