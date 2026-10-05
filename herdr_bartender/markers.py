"""State-dir flag files: DISABLED, DELIVERY_DOWN, heartbeat, pane markers, .failed."""

from __future__ import annotations

import os

from . import clock
from .log import log_debug
from .paths import ensure_private_dir, get_state_dir
from .sanitize import get_hex_pane_id


def is_disabled() -> bool:
    try:
        return (get_state_dir() / "DISABLED").exists()
    except Exception:
        return False


def touch_pane_failed(pane_id: str):
    if not pane_id or is_disabled():
        return
    try:
        panes_dir = ensure_private_dir(get_state_dir() / "panes")
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


def clear_pane_failed(pane_id: str):
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


def touch_pane_marker(pane_id: str):
    """Touch exactly one marker, panes/<hex(canonical pane)> (plan L14: no alias, no dual-linking)."""
    if not pane_id or is_disabled():
        return
    try:
        panes_dir = ensure_private_dir(get_state_dir() / "panes")
        hex_id = get_hex_pane_id(pane_id)
        with open(panes_dir / hex_id, "w", encoding="utf-8") as f:
            f.write(str(int(clock.time())))
        failed_path = panes_dir / f"{hex_id}.failed"
        if failed_path.exists():
            failed_path.unlink()
    except OSError as exc:
        log_debug(f"touch_pane_marker failed for {pane_id!r}: {exc}")


def refresh_pane_marker(pane_id: str) -> bool:
    """Reconciler heartbeat (Plan §5.1 item 5): refresh an EXISTING ``panes/<hex>``'s mtime; True when refreshed.

    Unlike ``touch_pane_marker`` it never clears ``<hex>.failed``, does nothing while that
    flag exists and never creates a marker (only a confirmed delivery does), so it is safe
    outside the cache lock: a delivery failure racing the heartbeat still leaves ``.failed``
    in place, and a marker removed by a concurrent confirmed Ended stays removed.
    """
    if not pane_id or is_disabled():
        return False
    try:
        panes_dir = get_state_dir() / "panes"
        hex_id = get_hex_pane_id(pane_id)
        if (panes_dir / f"{hex_id}.failed").exists():
            return False
        try:
            os.utime(panes_dir / hex_id, None)
        except FileNotFoundError:
            return False
        return True
    except OSError as exc:
        log_debug(f"Heartbeat could not refresh the marker of {pane_id!r}: {exc}")
        return False


def remove_pane_marker(pane_id: str):
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
