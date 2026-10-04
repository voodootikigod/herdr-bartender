"""Result envelopes written by senders and drained by the reconciler."""

from __future__ import annotations

import json
import os
import time  # real clock: result filenames must stay unique under a fake clock
from pathlib import Path

from . import clock
from .cache import BoundedSessionCache
from .log import log_debug
from .markers import remove_pane_marker
from .paths import get_state_dir
from .sender import ensure_reconciler_running, touch_reconciler_pending


def write_result_envelope(result_data: dict):
    try:
        results_dir = get_state_dir() / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        ns = time.time_ns()
        pid = os.getpid()
        seq = result_data.get("transmitting_seq", 0)
        fn = f"{ns:020d}_{pid}_{seq}.json"
        tmp_f = results_dir / f"{fn}.tmp"
        final_f = results_dir / fn
        with open(tmp_f, "w", encoding="utf-8") as f:
            json.dump(result_data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_f, final_f)
        touch_reconciler_pending()
        ensure_reconciler_running()
    except Exception as e:
        log_debug(f"Failed to write result envelope: {e}")


def drain_results_dir(state_dir: Path):
    results_dir = state_dir / "results"
    if not results_dir.exists():
        return
    cache_mgr = BoundedSessionCache(state_dir)
    for rf in sorted(results_dir.glob("*.json")):
        try:
            with open(rf, "r", encoding="utf-8") as rff:
                r_env = json.load(rff)
            r_sid = r_env.get("session_id")
            r_seq = r_env.get("transmitting_seq")
            r_state = r_env.get("transmitting_state")
            r_succ = r_env.get("success", False)
            with cache_mgr as data:
                s = data.get("sessions", {}).get(r_sid)
                p_id = r_env.get("pane_id")
                is_tomb = bool(p_id and p_id in data.get("tombstones", {}))
                if not s or (is_tomb and r_state != "Ended"):
                    if r_succ and r_state != "Ended":
                        data.setdefault("pending_compensations", []).append({
                            "session_id": r_sid,
                            "agent": "Herdr",
                            "generation": 1,
                            "admitted_at_ns": 0,
                            "timestamp": clock.time(),
                        })
                elif s and s.get("seq") == r_seq:
                    if r_succ:
                        s["delivered_state"] = r_state
                        s["delivered_seq"] = r_seq
                        s["delivery_status"] = "delivered"
                        if r_state == "Ended":
                            data.get("sessions", {}).pop(r_sid, None)
                            if p_id:
                                remove_pane_marker(p_id)
                    else:
                        s["delivery_status"] = "retryable_exhausted" if r_env.get("is_non_retryable") else "in_flight"
                cache_mgr.save(data)
            rf.unlink(missing_ok=True)
        except Exception:
            rf.unlink(missing_ok=True)
