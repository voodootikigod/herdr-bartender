"""Lock-free Herdr client helpers: SSH endpoint discovery and bounded snapshot queries.

All functions in this module enforce ``not runtime.IN_CRITICAL_SECTION`` before
performing any filesystem or subprocess I/O so the session cache lock is never
held across external reads or ``herdr`` invocations.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from . import boundedio, jsonsafe, runtime
from .log import log_debug
from .sanitize import sanitize_hostname

ENDPOINTS_MAX_BYTES = 262144
DEFAULT_SNAPSHOT_TIMEOUT = 2.5
MAX_MACHINE_ARG_LEN = 255


@dataclass(frozen=True)
class RemoteEndpoint:
    id: str
    host: str
    machine_arg: str
    label: str


def _guard_lock_free() -> None:
    if runtime.IN_CRITICAL_SECTION:
        raise AssertionError("herdr_client I/O attempted while holding the cache lock")


def _valid_machine_token(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > MAX_MACHINE_ARG_LEN or cleaned.startswith("-"):
        return None
    if any(ch.isspace() or ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F for ch in cleaned):
        return None
    return cleaned


def endpoints_file_path(xdg_state_home: Optional[Path] = None, home: Optional[Path] = None) -> Path:
    """Resolve ``${XDG_STATE_HOME}/herdr/client/endpoints.json`` (or ``~/.local/state/...``)."""
    if xdg_state_home is not None:
        base = Path(xdg_state_home)
    elif os.environ.get("XDG_STATE_HOME"):
        base = Path(os.environ["XDG_STATE_HOME"])
    else:
        resolved_home = Path(home) if home is not None else Path(os.path.expanduser("~"))
        base = resolved_home / ".local" / "state"
    return base / "herdr" / "client" / "endpoints.json"


def discover_enabled_remotes(
    xdg_state_home: Optional[Path] = None,
    home: Optional[Path] = None,
) -> Tuple[RemoteEndpoint, ...]:
    """Read enabled SSH remotes from Herdr's ``endpoints.json`` with zero subprocess spawns."""
    _guard_lock_free()
    path = endpoints_file_path(xdg_state_home, home)
    if os.path.islink(path) or not path.is_file():
        return ()
    try:
        raw = boundedio.read_regular_file(path, ENDPOINTS_MAX_BYTES)
        parsed = jsonsafe.loads(raw)
    except (OSError, ValueError) as exc:
        log_debug(f"Could not read Herdr endpoints.json ({exc})")
        return ()
    if not isinstance(parsed, dict):
        return ()
    ssh_entries = parsed.get("ssh")
    if not isinstance(ssh_entries, list):
        return ()

    endpoints: List[RemoteEndpoint] = []
    seen_hosts: set[str] = set()
    for entry in ssh_entries:
        if not isinstance(entry, dict) or entry.get("enabled") is not True:
            continue
        raw_target = entry.get("target")
        raw_id = entry.get("id")
        if raw_target not in (None, ""):
            machine_arg = _valid_machine_token(raw_target)
        else:
            machine_arg = _valid_machine_token(raw_id)
        if machine_arg is None:
            log_debug("Skipping invalid SSH remote target/id in endpoints.json")
            continue
        if raw_id not in (None, ""):
            ep_id = _valid_machine_token(raw_id)
            if ep_id is None:
                log_debug("Skipping invalid SSH remote id in endpoints.json")
                continue
        else:
            ep_id = machine_arg
        raw_label = entry.get("label")
        label = raw_label.strip()[:MAX_MACHINE_ARG_LEN] if isinstance(raw_label, str) and raw_label.strip() else ""
        target_str = raw_target.strip() if isinstance(raw_target, str) and raw_target.strip() else ""
        host = sanitize_hostname(label or target_str or ep_id)
        if host in seen_hosts:
            continue
        seen_hosts.add(host)
        endpoints.append(RemoteEndpoint(id=ep_id, host=host, machine_arg=machine_arg, label=label))
    return tuple(endpoints)


def fetch_herdr_snapshot(
    machine: Optional[str] = None,
    timeout: float = DEFAULT_SNAPSHOT_TIMEOUT,
) -> Optional[dict]:
    """Query ``herdr [--machine <m>] api snapshot`` outside the cache lock; None on error/empty."""
    _guard_lock_free()
    if machine is not None:
        clean_machine = _valid_machine_token(machine)
        if clean_machine is None:
            return None
        argv = ["herdr", "--machine", clean_machine, "api", "snapshot"]
    else:
        argv = ["herdr", "api", "snapshot"]

    try:
        res = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log_debug(f"herdr snapshot query failed ({argv}): {exc}")
        return None

    if res.returncode != 0 or not res.stdout or not res.stdout.strip():
        return None
    try:
        parsed = jsonsafe.loads(res.stdout)
    except ValueError as exc:
        log_debug(f"herdr snapshot returned invalid JSON ({argv}): {exc}")
        return None
    if not isinstance(parsed, dict):
        return None

    result_obj = parsed.get("result")
    if isinstance(result_obj, dict) and isinstance(result_obj.get("snapshot"), dict):
        snap = result_obj["snapshot"]
    elif isinstance(parsed.get("snapshot"), dict):
        snap = parsed["snapshot"]
    else:
        snap = parsed

    if isinstance(snap, dict) and isinstance(snap.get("panes"), list):
        return snap
    return None


__all__ = [
    "DEFAULT_SNAPSHOT_TIMEOUT",
    "ENDPOINTS_MAX_BYTES",
    "RemoteEndpoint",
    "discover_enabled_remotes",
    "endpoints_file_path",
    "fetch_herdr_snapshot",
]
