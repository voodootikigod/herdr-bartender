"""Hand-off helpers shared by every sender path.

The full Universal Sender Protocol (Step A/B/C) lands here in wave W3; for now
this module holds the reconciler hand-off primitives that all senders use.
"""

from __future__ import annotations

import os
import subprocess
import sys

from .paths import get_state_dir, launcher_path


def ensure_reconciler_running():
    if os.environ.get("HERDR_BARTENDER_UNIT_TESTING"):
        return
    try:
        subprocess.Popen(
            [sys.executable, str(launcher_path()), "--reconcile-background"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def touch_reconciler_pending():
    try:
        (get_state_dir() / "reconciler.pending").touch(exist_ok=True)
    except Exception:
        pass
