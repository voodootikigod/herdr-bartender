"""Process liveness, start-time lookups and Herdr/Bartender PID discovery.

All lookups go through PATH (`ps`, `pgrep`) so tests can shim them.
"""

from __future__ import annotations

import os
import re
import subprocess
import time

from . import runtime


def get_process_start_time(pid: int | None = None) -> str:
    target_pid = pid or os.getpid()
    try:
        res = subprocess.run(["ps", "-p", str(target_pid), "-o", "lstart="], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        if res.returncode == 0 and res.stdout.strip():
            raw_st = res.stdout.strip()
            try:
                clean_st = re.sub(r'\s+', ' ', raw_st)
                epoch_sec = int(time.mktime(time.strptime(clean_st, "%a %b %d %H:%M:%S %Y")))
                return str(epoch_sec)
            except Exception:
                return raw_st.replace(":", "-").replace(" ", "_")
    except Exception:
        pass
    return str(int(runtime.MODULE_LOAD_TIME))


def is_pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_process_instance_alive(pid: int | None, expected_start_time: str | None = None) -> bool:
    if not is_pid_alive(pid):
        return False
    if expected_start_time and expected_start_time != "None":
        st = get_process_start_time(pid)
        if st and st.strip() != expected_start_time.strip():
            return False
    return True


def get_herdr_pid() -> int | None:
    try:
        res = subprocess.run(["pgrep", "-xi", "herdr"], capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            pids = [int(p) for p in res.stdout.strip().split() if p.isdigit()]
            if not pids:
                return None
            # On macOS, check executable path to prioritize genuine GUI bundle (.app)
            gui_pids = []
            for p in pids:
                try:
                    comm_res = subprocess.run(["ps", "-p", str(p), "-o", "comm="], capture_output=True, text=True)
                    comm = comm_res.stdout.strip()
                    if ".app" in comm or "/Applications/" in comm:
                        gui_pids.append(p)
                except Exception:
                    pass
            candidate_pids = gui_pids if gui_pids else pids
            if len(candidate_pids) == 1:
                return candidate_pids[0]
            if len(candidate_pids) > 1:
                # Select the instance with the earliest process start time to avoid transient CLI tools
                oldest_pid = candidate_pids[0]
                oldest_ts = None
                for p in candidate_pids:
                    st = get_process_start_time(p)
                    try:
                        epoch_ts = float(st)
                    except Exception:
                        epoch_ts = float(p)
                    if oldest_ts is None or epoch_ts < oldest_ts:
                        oldest_ts = epoch_ts
                        oldest_pid = p
                return oldest_pid
        return None
    except Exception:
        return None


def is_herdr_alive() -> bool:
    if os.environ.get("HERDR_BARTENDER_UNIT_TESTING"):
        return True
    return get_herdr_pid() is not None


def get_herdr_instance_id() -> str:
    pid = get_herdr_pid()
    if pid is None:
        return ""
    start_time = get_process_start_time(pid)
    return f"{pid}:{start_time}"


def get_bartender_pid() -> int | None:
    try:
        for proc_name in ["Bartender 6", "Bartender"]:
            res = subprocess.run(["pgrep", "-x", proc_name], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            if res.returncode == 0:
                pids = [int(p) for p in res.stdout.strip().splitlines() if p.strip().isdigit()]
                if pids:
                    oldest_pid = None
                    oldest_ts = None
                    for p in pids:
                        st = get_process_start_time(p)
                        try:
                            clean_st = re.sub(r'\s+', ' ', st.strip())
                            epoch_ts = time.mktime(time.strptime(clean_st, "%a %b %d %H:%M:%S %Y"))
                        except Exception:
                            epoch_ts = float(p)
                        if oldest_ts is None or epoch_ts < oldest_ts:
                            oldest_ts = epoch_ts
                            oldest_pid = p
                    return oldest_pid
    except Exception:
        pass
    return None


def own_start_time() -> str:
    """Start time of this process, resolved lazily (never at import) and cached."""
    if runtime.PROCESS_START_TIME is None:
        runtime.PROCESS_START_TIME = get_process_start_time()
    return runtime.PROCESS_START_TIME


def reset_caches() -> None:
    """Drop cached liveness/start-time lookups (test hook; caches arrive in W2B)."""
    runtime.PROCESS_START_TIME = None
