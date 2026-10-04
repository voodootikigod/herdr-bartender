"""Injectable clock.

Production code calls ``clock.time()``, ``clock.time_ns()``, ``clock.monotonic()``
and ``clock.sleep()`` (always through the module attribute, never via
``from .clock import time``) for TTL, horizon, lease and deadline logic. Tests
replace these attributes (see ``tests/support/fake_clock.py``); by default they
delegate straight to the stdlib ``time`` module.
"""

from __future__ import annotations

import time as _time


def time() -> float:
    return _time.time()


def time_ns() -> int:
    return _time.time_ns()


def monotonic() -> float:
    return _time.monotonic()


def sleep(seconds: float) -> None:
    _time.sleep(seconds)
