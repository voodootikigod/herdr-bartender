"""Orphan export file ($HOME/.herdr-bartender-orphans.json) and replay."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

from .bridge import post_bartender_event
from .cache import BoundedSessionCache
from .config import SESSION_ID_REGEX
from .log import log_debug
from .markers import remove_pane_marker
from .paths import get_orphan_path, get_state_dir
from .sender import touch_reconciler_pending


def export_orphan_record(sid: str, session_dict: dict, orphan_file: Path | None = None):
    try:
        orphan_path = orphan_file or get_orphan_path()
        lock_path = orphan_path.with_name(orphan_path.name + ".lock")
        lock_fd = None
        try:
            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            orphans = {}
            if orphan_path.exists():
                try:
                    with open(orphan_path, "r", encoding="utf-8") as of:
                        data = json.load(of)
                        if isinstance(data, dict):
                            orphans = data.get("sessions", {})
                except Exception:
                    orphans = {}
            orphans[sid] = session_dict
            if len(orphans) > 256:
                orphans = dict(list(orphans.items())[-256:])
            tmp_orphan = orphan_path.with_name(f"{orphan_path.name}.tmp.{os.getpid()}")
            old_umask = os.umask(0o077)
            try:
                with open(tmp_orphan, "w", encoding="utf-8") as of:
                    json.dump({"version": 1, "sessions": orphans}, of, indent=2)
                    of.flush()
                    os.fsync(of.fileno())
                os.replace(tmp_orphan, orphan_path)
            finally:
                os.umask(old_umask)
        finally:
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    os.close(lock_fd)
                except Exception:
                    pass
    except Exception as e:
        log_debug(f"Failed to export orphan record {sid}: {e}")


def remove_orphan_record(sid: str, orphan_file: Path | None = None):
    try:
        orphan_path = orphan_file or get_orphan_path()
        if not orphan_path.exists():
            return
        lock_path = orphan_path.with_name(orphan_path.name + ".lock")
        lock_fd = None
        try:
            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            orphans = {}
            if orphan_path.exists():
                try:
                    with open(orphan_path, "r", encoding="utf-8") as of:
                        data = json.load(of)
                        if isinstance(data, dict):
                            orphans = data.get("sessions", {})
                except Exception:
                    orphans = {}
            if sid in orphans:
                orphans.pop(sid, None)
                if not orphans:
                    orphan_path.unlink(missing_ok=True)
                else:
                    tmp_orphan = orphan_path.with_name(f"{orphan_path.name}.tmp.{os.getpid()}")
                    old_umask = os.umask(0o077)
                    try:
                        with open(tmp_orphan, "w", encoding="utf-8") as of:
                            json.dump({"version": 1, "sessions": orphans}, of, indent=2)
                            of.flush()
                            os.fsync(of.fileno())
                        os.replace(tmp_orphan, orphan_path)
                    finally:
                        os.umask(old_umask)
        finally:
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    os.close(lock_fd)
                except Exception:
                    pass
    except Exception as e:
        log_debug(f"Failed to remove orphan record {sid}: {e}")


def run_replay_orphans(orphan_path_str: str, bridge_url: str | None = None) -> bool:
    orphan_path = Path(orphan_path_str).expanduser()
    if not orphan_path.exists():
        print(f"[-] Orphan file not found: {orphan_path}")
        return False

    lock_path = orphan_path.with_name(orphan_path.name + ".lock")
    lock_fd = None
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        try:
            with open(orphan_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[-] Failed to read orphan file: {e}")
            return False

        sessions = data.get("sessions", {}) if isinstance(data, dict) else {}
        if not sessions and isinstance(data, list):
            sessions = {s.get("session_id", f"orphan_{i}"): s for i, s in enumerate(data)}
        elif not sessions and isinstance(data, dict):
            sessions = data

        cache_mgr = BoundedSessionCache(get_state_dir())
        with cache_mgr as cdata:
            active_sessions = cdata.get("sessions", {})
            pane_gens = cdata.get("pane_generations", {})

        print(f"[*] Replaying {len(sessions)} orphaned session(s) from {orphan_path}...")
        all_success = True
        for sid, s in list(sessions.items()):
            if not isinstance(sid, str) or not SESSION_ID_REGEX.match(sid):
                print(f"    [-] Skipping orphan with invalid session_id: {sid}")
                sessions.pop(sid, None)
                continue

            orphan_gen = s.get("generation", 0) if isinstance(s, dict) else 0
            pane_id = s.get("pane_id") if isinstance(s, dict) else None
            if not pane_id and ":" in sid:
                parts = sid.split(":")
                pane_id = ":".join(parts[2:]) if len(parts) >= 3 else parts[-1]

            curr_gen = pane_gens.get(pane_id, 0)
            active_s = active_sessions.get(sid)
            if not active_s and pane_id:
                for asid, ainfo in active_sessions.items():
                    if ainfo.get("pane_id") == pane_id:
                        active_s = ainfo
                        break
            # Single normative boolean predicate:
            # Skip orphan iff an active, live (non-salvaged) session exists on the pane with desired_state != 'Ended'
            if active_s and not active_s.get("salvaged", False) and active_s.get("desired_state") != "Ended":
                print(f"    [*] Skipping orphan {sid}: pane currently has active non-Ended session {active_s.get('desired_state')}")
                sessions.pop(sid, None)
                continue

            agent_name = s.get("agent") if isinstance(s, dict) else "Herdr"
            payload = {
                "state": "Ended",
                "agent": agent_name or "Herdr",
                "session_id": sid,
            }
            success, is_non_retryable = post_bartender_event(payload, timeout=0.2, bridge_url=bridge_url)
            if success:
                print(f"    [+] Cleared {sid}")
                sessions.pop(sid, None)
                if pane_id:
                    remove_pane_marker(pane_id)
                with cache_mgr as cdata:
                    active_s = cdata.get("sessions", {}).get(sid)
                    if not active_s and pane_id:
                        for asid, ainfo in cdata.get("sessions", {}).items():
                            if ainfo.get("pane_id") == pane_id:
                                active_s = ainfo
                                break
                    if active_s and active_s.get("desired_state") != "Ended":
                        active_s["delivered_seq"] = 0
                        active_s["delivery_status"] = "in_flight"
                        touch_reconciler_pending()
                        cdata["consecutive_failures"] = 0
                        cache_mgr.save(cdata)
            else:
                print(f"    [-] Failed to deliver Ended for {sid}")
                all_success = False

        if all_success and not sessions:
            try:
                orphan_path.unlink()
                print(f"[+] Successfully cleared all orphaned sessions; removed {orphan_path}")
            except Exception:
                pass
        elif not all_success:
            try:
                tmp_orphan = orphan_path.with_name(f"{orphan_path.name}.tmp.{os.getpid()}")
                old_umask = os.umask(0o077)
                try:
                    with open(tmp_orphan, "w", encoding="utf-8") as of:
                        json.dump({"version": 1, "sessions": sessions}, of, indent=2)
                        of.flush()
                        os.fsync(of.fileno())
                    os.replace(tmp_orphan, orphan_path)
                finally:
                    os.umask(old_umask)
            except Exception:
                pass
        return all_success
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except Exception:
                pass
