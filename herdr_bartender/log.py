"""Debug log with 1MB rotation into the state dir."""

from __future__ import annotations

import time

from .paths import get_state_dir


def log_debug(msg: str):
    try:
        log_file = get_state_dir() / "plugin.log"
        if log_file.exists() and log_file.stat().st_size > 1_048_576:
            rot_file = get_state_dir() / "plugin.log.1"
            log_file.replace(rot_file)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass
