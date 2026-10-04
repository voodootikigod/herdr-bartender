"""Locked session cache (active-sessions.json) with corrupt-cache salvage."""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
from pathlib import Path

from . import clock, runtime
from .config import get_sanitized_hostname
from .log import log_debug
from .paths import get_orphan_path


class BoundedSessionCache:
    """Thread/Process-safe session cache with non-blocking bounded locking and quarantine."""

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.cache_file = state_dir / "active-sessions.json"
        self.lock_file = state_dir / "active-sessions.lock"
        self._lock_fd = None
        # Proactively sweep stale .guard_stdin.* files older than 60s on plugin initialization
        try:
            now_ts = clock.time()
            for g_tmp in self.state_dir.glob(".guard_stdin.*"):
                if now_ts - g_tmp.stat().st_mtime > 60:
                    g_tmp.unlink(missing_ok=True)
        except Exception:
            pass

    def __enter__(self):
        self._lock_fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        # Bounded acquisition with backoff dynamically scaled to remaining budget (up to 200ms)
        deadline = clock.monotonic() + min(0.2, max(0.02, runtime.time_remaining() - 0.3))
        acquired = False
        while clock.monotonic() < deadline:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (IOError, OSError):
                clock.sleep(0.01)
        if not acquired:
            log_debug("Warning: lock contention exceeded budget, proceeding with best effort")
        runtime.IN_CRITICAL_SECTION = True
        return self._load()

    def __exit__(self, exc_type, exc_val, exc_tb):
        runtime.IN_CRITICAL_SECTION = False
        try:
            if self._lock_fd is not None:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
        except Exception:
            pass

    def _load(self) -> dict:
        if not self.cache_file.exists():
            return {
                "version": 1,
                "host": get_sanitized_hostname(),
                "sessions": {},
                "tombstones": {},
                "agent_exits": {},
                "pane_generations": {},
                "dismissed_vendor_uuids": {},
                "next_generation": 1,
                "last_successful_delivery": 0.0,
                "consecutive_failures": 0,
            }
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, dict) or "sessions" not in data:
                    raise ValueError("Malformed cache structure")
                if "tombstones" not in data:
                    data["tombstones"] = {}
                if "agent_exits" not in data:
                    data["agent_exits"] = {}
                if "pane_generations" not in data:
                    data["pane_generations"] = {}
                if "dismissed_vendor_uuids" not in data:
                    data["dismissed_vendor_uuids"] = {}
                else:
                    d_uuids = data["dismissed_vendor_uuids"]
                    if len(d_uuids) > 64:
                        sorted_uuids = sorted(d_uuids.items(), key=lambda kv: kv[1].get("timestamp", 0) if isinstance(kv[1], dict) else 0)
                        for k, _ in sorted_uuids[:len(d_uuids) - 64]:
                            d_uuids.pop(k, None)
                if "next_generation" not in data:
                    data["next_generation"] = max([int(v) for v in data.get("pane_generations", {}).values()] + [1])
                return data
        except Exception as e:
            log_debug(f"Quarantining corrupt cache: {e}")
            salvaged = {}
            salvaged_pane_gens = {}
            try:
                # Selective spool preservation: protect close events so closed panes are ended; quarantine non-close status envelopes
                spool_dir = self.state_dir / "spool"
                bad_dir = spool_dir / "bad"
                if spool_dir.exists():
                    try:
                        bad_dir.mkdir(parents=True, exist_ok=True)
                        for sf in spool_dir.glob("*.json"):
                            try:
                                is_close_event = False
                                try:
                                    with open(sf, "r", encoding="utf-8") as sff:
                                        s_env = json.load(sff)
                                    ev_name = s_env.get("event_name", "")
                                    ev_state = s_env.get("event_data", {}).get("state", "")
                                    if ev_name in ("pane.closed", "tab.closed", "workspace.closed") or ev_state == "Ended":
                                        is_close_event = True
                                except Exception:
                                    pass
                                if not is_close_event:
                                    os.replace(sf, bad_dir / sf.name)
                            except Exception:
                                pass
                        # Prune bad_dir to max 20 files
                        bad_files = sorted([f for f in bad_dir.glob("*.json") if f.is_file()], key=lambda f: f.stat().st_mtime)
                        if len(bad_files) > 20:
                            for bf in bad_files[:len(bad_files) - 20]:
                                bf.unlink(missing_ok=True)
                    except Exception:
                        pass

                # Delete all existing pane markers on salvage so vendor hooks fall through immediately
                try:
                    panes_dir = self.state_dir / "panes"
                    if panes_dir.exists():
                        for mf in panes_dir.glob("*"):
                            if not mf.name.endswith(".vendor_active") and not mf.name.endswith(".failed"):
                                mf.unlink(missing_ok=True)
                except Exception:
                    pass

                # Best-effort salvage of candidate session IDs from corrupt raw text
                try:
                    with open(self.cache_file, "r", encoding="utf-8", errors="ignore") as cf:
                        raw_text = cf.read()
                        valid_id_regex = re.compile(r'^herdr:[a-zA-Z0-9_-]{1,32}:[a-zA-Z0-9_:-]{1,48}$')
                        matches = re.findall(r'herdr:[a-zA-Z0-9_-]{1,32}:[a-zA-Z0-9_:-]{1,48}', raw_text)
                        now_salvage = clock.time()
                        now_salvage_ns = clock.time_ns()
                        salvage_epoch_gen = max(int(now_salvage), 1_700_000_000)
                        gen_match = re.search(r'"pane_generations"\s*:\s*\{([^}]*)\}', raw_text)
                        if gen_match:
                            for pair in gen_match.group(1).split(","):
                                if ":" in pair:
                                    k, v = pair.split(":", 1)
                                    clean_k = k.strip().strip('"\'')
                                    clean_v = v.strip()
                                    if clean_v.isdigit():
                                        salvaged_pane_gens[clean_k] = int(clean_v)
                        for m in set(matches):
                            if not valid_id_regex.match(m):
                                continue
                            parts = m.split(":")
                            pane_id = ":".join(parts[2:]) if len(parts) >= 3 else parts[-1]
                            ws_id = pane_id.split(":", 1)[0] if ":" in pane_id else None
                            cur_gen = max(salvaged_pane_gens.get(pane_id, 0), salvage_epoch_gen)
                            salvaged_pane_gens[pane_id] = cur_gen
                            # All salvaged sessions stage quiescently as Idle without regex text search
                            salvaged[m] = {
                                "pane_id": pane_id,
                                "workspace_id": ws_id,
                                "tab_id": None,
                                "host": parts[1] if len(parts) >= 2 else get_sanitized_hostname(),
                                "agent": "Herdr",
                                "raw_agent": None,
                                "title": f"Salvaged Session {pane_id}",
                                "cwd": "",
                                "desired_state": "Idle",
                                "delivered_state": "Idle",
                                "seq": 1,
                                "delivered_seq": 1,
                                "rejected_seq": 0,
                                "generation": cur_gen,
                                "desired_payload": {
                                    "state": "Idle",
                                    "agent": "Herdr",
                                    "session_id": m,
                                    "seq": 1,
                                },
                                "delivery_status": "salvaged",
                                "delivery_error": None,
                                "delivery_attempts": 0,
                                "salvaged": True,
                                "admitted_at_ns": now_salvage_ns,
                                "last_event_ns": now_salvage_ns,
                                "last_arrival_ns": now_salvage_ns,
                                "last_event_at": now_salvage,
                                "last_applied_arrival_time": now_salvage,
                            }
                except Exception:
                    pass
                corrupt_path = self.state_dir / f"active-sessions.json.corrupt.{int(clock.time())}"
                self.cache_file.replace(corrupt_path)
            except Exception:
                pass
            return {
                "version": 1,
                "host": get_sanitized_hostname(),
                "sessions": salvaged,
                "tombstones": {},
                "pane_generations": salvaged_pane_gens,
                "next_generation": max(list(salvaged_pane_gens.values()) + [salvage_epoch_gen]),
                "last_successful_delivery": 0.0,
                "consecutive_failures": 0,
                "cache_seq": 1,
            }

    def save(self, data: dict):
        runtime.IN_CRITICAL_SECTION = True
        try:
            data["last_updated"] = clock.time()
            if "host" not in data:
                data["host"] = get_sanitized_hostname()
            data["cache_seq"] = int(data.get("cache_seq", 0)) + 1
            if "next_generation" not in data:
                data["next_generation"] = max([int(v) for v in data.get("pane_generations", {}).values()] + [1])
            now_ns = clock.time_ns()
            tombstones = data.get("tombstones", {})
            cleaned_tombstones = {}
            for k, v in tombstones.items():
                c_ns = v.get("closed_at_ns", 0) if isinstance(v, dict) else int(v)
                if (now_ns - c_ns) <= 60_000_000_000:
                    cleaned_tombstones[k] = v
            data["tombstones"] = cleaned_tombstones

            sessions = data.get("sessions", {})
            if len(sessions) > 256:
                sorted_s = sorted(sessions.items(), key=lambda kv: kv[1].get("last_event_at", 0))
                for sid, s in sorted_s:
                    if len(sessions) <= 256:
                        break
                    is_safe_ended = (s.get("desired_state") == "Ended" and (
                        s.get("delivered_state") == "Ended" or 
                        s.get("delivered_seq") == s.get("seq") or 
                        s.get("orphaned_ended", False)
                    ))
                    if is_safe_ended or s.get("salvaged", False):
                        sessions.pop(sid, None)

            pane_gens = data.get("pane_generations", {})
            if len(pane_gens) > 512:
                active_panes = {s.get("pane_id") for s in sessions.values()} | set(cleaned_tombstones.keys())
                orphan_panes = set()
                try:
                    orphan_path = get_orphan_path()
                    if orphan_path.exists():
                        with open(orphan_path, "r", encoding="utf-8") as of:
                            odata = json.load(of)
                            for osid, osess in odata.get("sessions", {}).items():
                                opid = osess.get("pane_id")
                                if opid:
                                    orphan_panes.add(opid)
                except Exception:
                    pass
                protected_panes = active_panes | orphan_panes
                inactive_gens = [(pid, gen) for pid, gen in pane_gens.items() if pid not in protected_panes]
                inactive_gens.sort(key=lambda kv: kv[1])
                for pid, _ in inactive_gens:
                    if len(pane_gens) <= 512:
                        break
                    pane_gens.pop(pid, None)

            tmp_path = self.state_dir / f"active-sessions.json.tmp.{os.getpid()}"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.cache_file)
        except Exception as e:
            log_debug(f"Error saving cache: {e}")
        finally:
            runtime.IN_CRITICAL_SECTION = False
            if runtime.PENDING_WATCHDOG_EXIT:
                log_debug("Exiting cleanly after deferred watchdog deadline")
                sys.exit(0)
