"""Vendor (.vendor_active) dismissal and cleanup."""

from __future__ import annotations

import json

from . import clock
from .bridge import _raw_post_event
from .cache import BoundedSessionCache
from .paths import get_state_dir
from .sanitize import get_hex_pane_id


def cleanup_vendor_active(pane_id: str, raw_pane_id: str | None = None, is_pane_closed: bool = False, bridge_url: str | None = None):
    """Dismisses stranded vendor entries from Top Shelf by reading vendor_session_id.
    If .vendor_active has no UUID (bare touch):
      - If is_pane_closed is True or mtime > 60s: unlinks the file so it does not outlive the pane.
      - If active session and mtime <= 60s: retains it, allowing vendor session-terminal hook to clean up.
    """
    try:
        panes_dir = get_state_dir() / "panes"
        hex_id = get_hex_pane_id(pane_id)
        vendor_file = panes_dir / f"{hex_id}.vendor_active"
        if vendor_file.exists():
            try:
                with open(vendor_file, "r", encoding="utf-8") as vf:
                    v_content = vf.read().strip()
                if v_content.startswith("{"):
                    v_data = json.loads(v_content)
                    v_sid = v_data.get("vendor_session_id")
                    if v_sid:
                        _raw_post_event(
                            {"state": "Ended", "session_id": v_sid, "agent": "Vendor"},
                            timeout=0.2,
                            bridge_url=bridge_url
                        )
                        vendor_file.unlink(missing_ok=True)
                        try:
                            cache_mgr = BoundedSessionCache(get_state_dir())
                            with cache_mgr as data:
                                data.setdefault("dismissed_vendor_uuids", {})[v_sid] = {
                                    "timestamp": clock.time(),
                                    "pane_hex": hex_id,
                                }
                                cache_mgr.save(data)
                        except Exception:
                            pass
                else:
                    # Bare touch (no JSON UUID): on confirmed Herdr delivery or pane close, unlink to restore dedup
                    vendor_file.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass
