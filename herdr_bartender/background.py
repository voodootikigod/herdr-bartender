"""--reconcile-background singleton loop."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess

from . import clock
from .bridge import check_bridge_health
from .cache import BoundedSessionCache
from .hooks import get_vendor_hooks_dir, install_hooks, verify_vendor_hooks_intact
from .log import log_debug
from .markers import clear_delivery_down, is_delivery_down, is_disabled, touch_pane_marker
from .orphans import export_orphan_record, run_replay_orphans
from .paths import get_orphan_path, get_state_dir
from .process import get_bartender_pid, is_herdr_alive
from .reconciler import reconcile_active_sessions
from .results import drain_results_dir
from .spool import replay_spool_dir


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

    try:
        cache_mgr = BoundedSessionCache(state_dir)
        bartender_absent_since: float | None = None
        while not is_disabled():
            if pending_file.exists():
                try:
                    pending_file.unlink()
                except Exception:
                    pass

            drain_results_dir(state_dir)
            replay_spool_dir(state_dir, bridge_url=bridge_url)
            reconcile_active_sessions(state_dir, bridge_url=bridge_url)

            # Automatic orphan replay when bridge is healthy and orphan file exists
            orphan_path = get_orphan_path()
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

            # Sweep stale .guard_stdin.* files older than 60s
            now_sweep = clock.time()
            for g_tmp in state_dir.glob(".guard_stdin.*"):
                try:
                    if now_sweep - g_tmp.stat().st_mtime > 60:
                        g_tmp.unlink(missing_ok=True)
                except Exception:
                    pass


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

            # If pending file was touched during the sweep pass, re-run loop immediately
            if pending_file.exists():
                bartender_absent_since = None
                continue

            spool_dir = state_dir / "spool"
            has_spool = spool_dir.exists() and any(spool_dir.glob("*.json"))
            if loop_once or (not pending_file.exists() and not has_spool and active_count == 0 and exhausted_count == 0 and not is_delivery_down()):
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
                    orphans_to_export = {}
                    with cache_mgr as data:
                        orphans_to_export = {
                            sid: dict(s) for sid, s in data.get("sessions", {}).items()
                            if s.get("delivered_state") != "Ended"
                        }
                    for sid, s in orphans_to_export.items():
                        export_orphan_record(sid, s)
                    break
            else:
                bartender_absent_since = None

            sleep_time = 300.0 if (bartender_absent_since is not None and clock.time() - bartender_absent_since > 1000) else 20.0
            sleep_deadline = clock.time() + sleep_time
            while clock.time() < sleep_deadline and not is_disabled():
                if pending_file.exists() or (spool_dir.exists() and any(spool_dir.glob("*.json"))):
                    bartender_absent_since = None
                    break
                clock.sleep(0.5)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        except Exception:
            pass
