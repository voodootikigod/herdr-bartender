"""Process liveness, start-time lookups and Herdr/Bartender PID discovery.

All lookups go through PATH (`ps`, `pgrep`) so tests can shim them. Every probe:

* runs with ``LC_ALL=C`` and a timeout (``probe_timeout()``): on the
  watchdog-bounded event path ``min(0.25, time_remaining() - 0.3)`` (Plan §6.1),
  in unbounded modes (reconciler, --cleanup, --replay-orphans) a fixed 1.0s.
  A timed-out or failing probe yields *unknown* (``None``), never a fabricated
  value, and ends that lookup (a hung ``pgrep`` is not retried with other names);
* is memoised for ``LIVENESS_TTL_SECONDS`` (0.5s, Plan §6.1 L706) on the
  injectable monotonic clock. ``reset_caches()`` drops the memo (test hook).

Start times are integer epoch strings parsed from ``ps -o lstart=``. An unknown
start time is ``None`` and carries no information (R14): it never signals a
restart and never rejects a lease holder.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

from . import clock, runtime
from .log import log_debug

LIVENESS_TTL_SECONDS = 0.5
# Expected latency: Linux `pgrep` ~50ms with ~800 processes; macOS `pgrep -f` reads
# KERN_PROCARGS2 for every process and can take 100-200ms under load. The hot-path cap
# leaves headroom for that while two probes still fit the 1.4s budget with the network.
HOT_PATH_PROBE_TIMEOUT = 0.25   # per-probe cap inside the watchdog-bounded plugin process
RELAXED_PROBE_TIMEOUT = 1.0     # per-probe timeout for unbounded modes (reconciler, CLI tools)
MIN_PROBE_TIMEOUT = 0.02
PROBE_BUDGET_RESERVE = 0.3      # same reserve the network budget keeps (Plan §6.1)

HERDR_PROCESS_NAME = "herdr"
# R15: identical to hook_guard.sh — the bundle path must be the executable (argv[0]), never an
# argument; tests/test_process.py asserts the guard embeds this exact pattern.
HERDR_APP_PATTERN = r"^[^[:space:]]*/Herdr\.app/Contents/MacOS/"
BARTENDER_PROCESS_NAMES = ("Bartender 6", "Bartender")

_MONTHS = {name: i for i, name in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1)}
_WEEKDAYS = frozenset(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))


class ProcessProbe(NamedTuple):
    """Result of a process-table probe: ``known`` is False when the probe itself failed."""

    pid: Optional[int]
    known: bool


# key -> (monotonic timestamp, value). Replaced wholesale, never mutated in place.
_CACHE: Dict[Tuple, Tuple[float, object]] = {}
# Own start time is resolved at most once successfully (see own_start_time()).
_OWN_IDENTITY_RESOLVED = False


# -- cache -----------------------------------------------------------------------
def reset_caches() -> None:
    """Drop memoised liveness/start-time lookups and our own resolved start time (test hook)."""
    global _CACHE, _OWN_IDENTITY_RESOLVED
    _CACHE = {}
    _OWN_IDENTITY_RESOLVED = False
    runtime.PROCESS_START_TIME = None


def _memo(key: Tuple) -> Tuple[bool, object]:
    """(True, value) for a fresh memoised lookup, else (False, None). Never spawns."""
    hit = _CACHE.get(key)
    if hit is not None and clock.monotonic() - hit[0] < LIVENESS_TTL_SECONDS:
        return True, hit[1]
    return False, None


def _cached(key: Tuple, compute: Callable[[], object]) -> object:
    global _CACHE
    found, value = _memo(key)
    if found:
        return value
    value = compute()
    _CACHE = {**_CACHE, key: (clock.monotonic(), value)}
    return value


# -- subprocess probes -----------------------------------------------------------
def probe_timeout() -> float:
    """Per-probe subprocess timeout (Plan §6.1).

    Bounded path: ``max(0.02, min(0.25, time_remaining() - 0.3))``. Unbounded modes
    get a fixed RELAXED_PROBE_TIMEOUT: their time_remaining() is pinned at its floor.
    """
    if not runtime.deadline_bounded():
        return RELAXED_PROBE_TIMEOUT
    budget = runtime.time_remaining() - PROBE_BUDGET_RESERVE
    return max(MIN_PROBE_TIMEOUT, min(HOT_PATH_PROBE_TIMEOUT, budget))


def _c_locale_env() -> Dict[str, str]:
    return {**os.environ, "LC_ALL": "C", "LANG": "C"}


def _run_probe(argv: List[str]) -> Optional[subprocess.CompletedProcess]:
    """Run a ps/pgrep probe; None when it fails to run or times out."""
    try:
        return subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=probe_timeout(), env=_c_locale_env(),
        )
    except subprocess.TimeoutExpired:
        log_debug(f"Process probe timed out: {argv[0]}")
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        log_debug(f"Process probe failed: {argv[0]}: {e}")
    return None


def _pgrep(args: List[str]) -> Optional[List[int]]:
    """PIDs matched by pgrep: [] when nothing matches (exit 1), None when the probe failed."""
    res = _run_probe(["pgrep", *args])
    if res is None or res.returncode not in (0, 1):
        return None
    if res.returncode == 1:
        return []
    return sorted({int(tok) for tok in res.stdout.split() if tok.isdigit()})


def _ps_field(pid: int, field: str) -> Optional[str]:
    res = _run_probe(["ps", "-p", str(pid), "-o", f"{field}="])
    if res is None or res.returncode != 0:
        return None
    value = res.stdout.strip()
    return value or None


# -- start times -----------------------------------------------------------------
def parse_lstart(text: Optional[str]) -> Optional[str]:
    """Parse C-locale ``ps -o lstart=`` output ("Sat Oct  4 09:00:00 2026") to an integer epoch string."""
    if not text:
        return None
    parts = text.split()
    if len(parts) != 5 or parts[0] not in _WEEKDAYS or parts[1] not in _MONTHS:
        return None
    hms = parts[3].split(":")
    if len(hms) != 3 or not all(p.isdigit() for p in hms) or not (parts[2].isdigit() and parts[4].isdigit()):
        return None
    hour, minute, second = (int(p) for p in hms)
    day, year = int(parts[2]), int(parts[4])
    if not (1 <= day <= 31 and hour <= 23 and minute <= 59 and second <= 60):
        return None
    try:
        return str(int(time.mktime((year, _MONTHS[parts[1]], day, hour, minute, second, 0, 0, -1))))
    except (OverflowError, ValueError):
        return None


def known_start_time(value: object) -> Optional[str]:
    """Normalise a stored start time: None, "" and "None" (as embedded in lease tokens) are unknown."""
    text = str(value).strip() if value is not None else ""
    return None if text in ("", "None") else text


def start_times_differ(a: object, b: object) -> bool:
    """R14: True only when both start times are known and different."""
    ka, kb = known_start_time(a), known_start_time(b)
    return ka is not None and kb is not None and ka != kb


def get_process_start_time(pid: Optional[int] = None) -> Optional[str]:
    """Integer epoch start time of ``pid`` (default: this process), or None when unknown."""
    target = os.getpid() if pid is None else pid
    if not isinstance(target, int) or target <= 0:
        return None
    return _cached(("start", target), lambda: parse_lstart(_ps_field(target, "lstart")))


def own_start_time() -> Optional[str]:
    """Start time of this process, resolved lazily and cached.

    Never spawns ``ps`` inside a cache-lock critical section: call
    ``warm_process_identity()`` before locking. Unresolved inside a critical
    section, it returns None (an unknown start time, R14).
    """
    global _OWN_IDENTITY_RESOLVED
    if _OWN_IDENTITY_RESOLVED:
        return runtime.PROCESS_START_TIME
    if runtime.IN_CRITICAL_SECTION:
        log_debug("own_start_time() unresolved inside critical section; using unknown start time")
        return None
    resolved = get_process_start_time(os.getpid())
    if resolved is not None:
        runtime.PROCESS_START_TIME = resolved
        _OWN_IDENTITY_RESOLVED = True
    return resolved


def warm_process_identity() -> Optional[str]:
    """Resolve this process's identity (start time) before any cache lock is taken."""
    return own_start_time()


# -- liveness --------------------------------------------------------------------
def is_pid_alive(pid: Optional[int]) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False


def is_process_instance_alive(pid: Optional[int], expected_start_time: Optional[str] = None,
                              start_time: Optional[Callable[[Optional[int]], Optional[str]]] = None) -> bool:
    """PID is alive and, when both start times are known, it is still the same process instance.

    ``start_time`` looks up the PID's start time (default: ``get_process_start_time``,
    which may spawn ``ps``); under the cache lock pass ``memoised_start_time``.
    """
    if not is_pid_alive(pid):
        return False
    if known_start_time(expected_start_time) is None:
        return True
    lookup = get_process_start_time if start_time is None else start_time
    return not start_times_differ(lookup(pid), expected_start_time)


# -- memo-only lookups (safe under the cache lock: they never spawn) --------------------
def memoised_start_time(pid: Optional[int]) -> Optional[str]:
    """``get_process_start_time(pid)`` from the 0.5s memo only; not memoised is unknown (None, R14)."""
    found, value = _memo(("start", pid))
    return value if found else None  # type: ignore[return-value]


def memoised_instance_alive(pid: Optional[int], expected_start_time: Optional[str] = None) -> bool:
    """``is_process_instance_alive`` without spawning: an unresolved start time never rejects a holder (R14)."""
    return is_process_instance_alive(pid, expected_start_time, start_time=memoised_start_time)


def memoised_herdr_alive() -> bool:
    """``is_herdr_alive()`` from the 0.5s memo only; not memoised counts as alive (same as a failed probe)."""
    found, probe = _memo(("herdr",))
    return not (found and probe.pid is None and probe.known)  # type: ignore[union-attr]


def _earliest(pids: List[int]) -> Optional[int]:
    """PID with the earliest known start time; unknown start times rank last, ties by lowest PID."""
    if not pids:
        return None

    def rank(pid: int) -> Tuple[int, int, int]:
        st = get_process_start_time(pid)
        return (0, int(st), pid) if st is not None else (1, 0, pid)

    return min(pids, key=rank)


def _is_gui_bundle(pid: int) -> bool:
    comm = _ps_field(pid, "comm") or ""
    return ".app/" in comm or comm.startswith("/Applications/")


def _probe_herdr() -> ProcessProbe:
    by_name = _pgrep(["-xi", HERDR_PROCESS_NAME])
    if by_name is None:
        return ProcessProbe(None, False)  # pgrep failing/hung: do not spend more budget on it
    by_bundle = _pgrep(["-f", HERDR_APP_PATTERN])
    candidates = sorted(set(by_name) | set(by_bundle or []))
    if not candidates:
        return ProcessProbe(None, by_bundle is not None)
    gui = [p for p in candidates if _is_gui_bundle(p)]
    return ProcessProbe(_earliest(gui or candidates), True)


def probe_herdr() -> ProcessProbe:
    """Plan §3.4: GUI `.app` bundle first, then earliest start (R15 matching rule), memoised 0.5s."""
    return _cached(("herdr",), _probe_herdr)


def get_herdr_pid() -> Optional[int]:
    return probe_herdr().pid


def herdr_liveness() -> Optional[bool]:
    """True/False when known; None when the process probe itself failed."""
    probe = probe_herdr()
    if probe.pid is not None:
        return True
    return False if probe.known else None


def is_herdr_alive() -> bool:
    """Herdr liveness; an unknown probe result counts as alive (never mass-expire on a probe failure)."""
    return herdr_liveness() is not False


def _probe_bartender() -> ProcessProbe:
    for name in BARTENDER_PROCESS_NAMES:
        pids = _pgrep(["-x", name])
        if pids is None:
            return ProcessProbe(None, False)  # pgrep failing/hung: unknown, stop probing
        if pids:
            return ProcessProbe(_earliest(pids), True)
    return ProcessProbe(None, True)


def probe_bartender() -> ProcessProbe:
    """Plan §1 L134: `Bartender 6` then `Bartender`, earliest start time, memoised 0.5s."""
    return _cached(("bartender",), _probe_bartender)


def get_bartender_pid() -> Optional[int]:
    return probe_bartender().pid


def get_herdr_instance_id() -> str:
    pid = get_herdr_pid()
    if pid is None:
        return ""
    return f"{pid}:{get_process_start_time(pid) or ''}"
