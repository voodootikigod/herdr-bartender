"""State-dir flag files: DISABLED, DELIVERY_DOWN, heartbeat, pane markers, .failed."""

from __future__ import annotations

import os

from . import clock
from .paths import get_state_dir
from .sanitize import get_hex_pane_id


def is_disabled() -> bool:
    try:
        return (get_state_dir() / "DISABLED").exists()
    except Exception:
        return False


def touch_pane_failed(pane_id: str, raw_pane_id: str | None = None):
    if not pane_id or is_disabled():
        return
    try:
        panes_dir = get_state_dir() / "panes"
        panes_dir.mkdir(parents=True, exist_ok=True)
        now_ts = str(int(clock.time()))
        hex_id = get_hex_pane_id(pane_id)
        failed_path = panes_dir / f"{hex_id}.failed"
        with open(failed_path, "w", encoding="utf-8") as f:
            f.write(now_ts)
        marker_path = panes_dir / hex_id
        if marker_path.exists():
            marker_path.unlink()
    except Exception:
        pass


def clear_pane_failed(pane_id: str, raw_pane_id: str | None = None):
    if not pane_id:
        return
    try:
        hex_id = get_hex_pane_id(pane_id)
        failed = get_state_dir() / "panes" / f"{hex_id}.failed"
        if failed.exists():
            failed.unlink()
    except Exception:
        pass


def touch_delivery_down():
    try:
        (get_state_dir() / "DELIVERY_DOWN").touch(exist_ok=True)
    except Exception:
        pass


def clear_delivery_down():
    try:
        flag = get_state_dir() / "DELIVERY_DOWN"
        if flag.exists():
            flag.unlink()
    except Exception:
        pass


def is_delivery_down() -> bool:
    try:
        return (get_state_dir() / "DELIVERY_DOWN").exists()
    except Exception:
        return False


def touch_heartbeat():
    if is_disabled():
        return
    try:
        marker = get_state_dir() / "plugin-active"
        marker.touch(exist_ok=True)
    except Exception:
        pass


def remove_heartbeat():
    try:
        marker = get_state_dir() / "plugin-active"
        if marker.exists():
            marker.unlink()
    except Exception:
        pass


def touch_pane_marker(pane_id: str, raw_pane_id: str | None = None):
    if not pane_id or is_disabled():
        return
    try:
        panes_dir = get_state_dir() / "panes"
        panes_dir.mkdir(parents=True, exist_ok=True)
        now_ts = str(int(clock.time()))
        hex_ids = [get_hex_pane_id(pane_id)]
        if raw_pane_id and ":" not in raw_pane_id:
            env_ws = os.environ.get("HERDR_WORKSPACE_ID") or "default"
            alt_id = f"{env_ws}:{raw_pane_id}"
            if alt_id != pane_id:
                hex_ids.append(get_hex_pane_id(alt_id))
        for hex_id in hex_ids:
            marker_path = panes_dir / hex_id
            with open(marker_path, "w", encoding="utf-8") as f:
                f.write(now_ts)
            failed_path = panes_dir / f"{hex_id}.failed"
            if failed_path.exists():
                failed_path.unlink()
    except Exception:
        pass


def remove_pane_marker(pane_id: str, raw_pane_id: str | None = None):
    if not pane_id:
        return
    try:
        hex_id = get_hex_pane_id(pane_id)
        marker = get_state_dir() / "panes" / hex_id
        if marker.exists():
            marker.unlink()
        failed = get_state_dir() / "panes" / f"{hex_id}.failed"
        if failed.exists():
            failed.unlink()
    except Exception:
        pass
