"""Deterministic replacement for herdr_bartender.clock."""

from __future__ import annotations

import time
import unittest
from unittest import mock

from herdr_bartender import clock


class FakeClock:
    """Frozen wall + monotonic clock. ``sleep`` advances time instead of blocking."""

    def __init__(self, start: float | None = None, monotonic_start: float | None = None) -> None:
        self._wall_ns = int((time.time() if start is None else start) * 1e9)
        self._mono = time.monotonic() if monotonic_start is None else monotonic_start
        self.sleeps: list = []

    # clock API -------------------------------------------------------------
    def time(self) -> float:
        return self._wall_ns / 1e9

    def time_ns(self) -> int:
        return self._wall_ns

    def monotonic(self) -> float:
        return self._mono

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(max(0.0, seconds))

    # controls ----------------------------------------------------------------
    def advance(self, seconds: float) -> None:
        self._wall_ns += int(seconds * 1e9)
        self._mono += seconds

    def set_time(self, wall: float) -> None:
        """Jump the wall clock (either direction); monotonic time is untouched."""
        self._wall_ns = int(wall * 1e9)

    def step_back(self, seconds: float) -> None:
        """Simulate a backward wall-clock adjustment (NTP step)."""
        self._wall_ns -= int(seconds * 1e9)

    def install(self, testcase: unittest.TestCase) -> "FakeClock":
        for name in ("time", "time_ns", "monotonic", "sleep"):
            patcher = mock.patch.object(clock, name, getattr(self, name))
            patcher.start()
            testcase.addCleanup(patcher.stop)
        return self
