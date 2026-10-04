"""Spool directory: enqueue and FIFO replay of deferred events."""

from __future__ import annotations

import json
import os
import time  # real monotonic_ns keeps spool filenames unique under a fake clock
from pathlib import Path

from . import clock
from .bridge import post_bartender_event
from .handlers import (
    handle_agent_status_changed,
    handle_pane_closed,
    handle_tab_closed,
    handle_workspace_closed,
)
from .log import log_debug
from .paths import get_state_dir


def enqueue_spool(event_name: str, event_data: dict, context: dict, arrival_time: float | None = None, arrival_ns: int | None = None):
    try:
        spool_dir = get_state_dir() / "spool"
        spool_dir.mkdir(parents=True, exist_ok=True)
        existing_spools = sorted([f for f in spool_dir.glob("*.json") if f.is_file()])
        if len(existing_spools) >= 100:
            # Protect close events from spool pruning; only prune oldest non-close events
            to_prune = len(existing_spools) - 99
            pruned = 0
            for old_spool in existing_spools:
                if pruned >= to_prune:
                    break
                try:
                    with open(old_spool, "r", encoding="utf-8") as sf:
                        s_env = json.load(sf)
                    ev_name = s_env.get("event_name", "")
                    ev_state = s_env.get("event_data", {}).get("state", "")
                    if ev_name in ("pane.closed", "tab.closed", "workspace.closed") or ev_state == "Ended":
                        continue
                    old_spool.unlink(missing_ok=True)
                    pruned += 1
                except Exception:
                    old_spool.unlink(missing_ok=True)
                    pruned += 1
        arr_time = arrival_time or clock.time()
        arr_ns = arrival_ns or clock.time_ns()
        envelope = {
            "event_name": event_name,
            "event_data": event_data,
            "context": context,
            "arrival_time": arr_time,
            "arrival_ns": arr_ns,
            "enqueued_at": clock.time(),
            "enqueued_ns": arr_ns,
        }
        filename = f"{arr_ns:020d}_{os.getpid()}_{time.monotonic_ns()}.json"
        tmp_file = spool_dir / f"{filename}.tmp"
        final_file = spool_dir / filename
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(envelope, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, final_file)
    except Exception as e:
        log_debug(f"Failed to enqueue spool: {e}")


def replay_spool_dir(state_dir: Path, bridge_url: str | None = None, max_batch: int = 16):
    spool_dir = state_dir / "spool"
    if not spool_dir.exists():
        return
    bad_dir = spool_dir / "bad"
    if bad_dir.exists():
        try:
            bad_files = sorted([f for f in bad_dir.glob("*.json") if f.is_file()], key=lambda f: f.stat().st_mtime)
            if len(bad_files) >= 20:
                for bf in bad_files[: len(bad_files) - 19]:
                    bf.unlink(missing_ok=True)
        except Exception:
            pass
    files = sorted([f for f in spool_dir.glob("*.json") if f.is_file()])
    for file_path in files[:max_batch]:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                envelope = json.load(f)
            ename = envelope.get("event_name", "")
            edata = envelope.get("event_data", {})
            econtext = envelope.get("context", {})
            arr_time = envelope.get("arrival_time", envelope.get("enqueued_at", clock.time()))
            arr_ns = envelope.get("arrival_ns", envelope.get("enqueued_ns", int(arr_time * 1e9)))
            if ename == "pane.agent_status_changed":
                handle_agent_status_changed(edata, econtext, bridge_url=bridge_url, arrival_time=arr_time, arrival_ns=arr_ns)
            elif ename == "pane.closed":
                handle_pane_closed(edata, econtext, bridge_url=bridge_url, arrival_ns=arr_ns)
            elif ename == "tab.closed":
                handle_tab_closed(edata, econtext, bridge_url=bridge_url, arrival_ns=arr_ns)
            elif ename == "workspace.closed":
                handle_workspace_closed(edata, econtext, bridge_url=bridge_url, arrival_ns=arr_ns)
            elif ename == "direct_post":
                post_bartender_event(edata, bridge_url=bridge_url)
            file_path.unlink(missing_ok=True)
        except Exception as e:
            log_debug(f"Error replaying spool file {file_path}, quarantining to spool/bad: {e}")
            try:
                bad_dir.mkdir(parents=True, exist_ok=True)
                os.replace(file_path, bad_dir / file_path.name)
            except Exception:
                file_path.unlink(missing_ok=True)
