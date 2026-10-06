"""Helpers for reconciler / lifecycle tests: seeded session records and a FakeClock-driven loop runner."""

from __future__ import annotations

import json
from typing import Callable, List, Optional
from unittest import mock

from herdr_bartender import background, clock
from herdr_bartender.sanitize import get_hex_pane_id


def session(pane: str, state: str = "Working", *, seq: int = 1, delivered: bool = True, now: float,
            agent: str = "Claude (Herdr)", **extra) -> dict:
    """A cached session record as the event path leaves it (delivered, or in flight)."""
    record = {
        "pane_id": pane, "workspace_id": pane.split(":", 1)[0], "agent": agent, "title": f"T {pane}",
        "desired_state": state, "seq": seq, "delivered_seq": seq if delivered else seq - 1,
        "delivered_state": state if delivered else None,
        "delivery_status": "delivered" if delivered else "in_flight", "delivery_attempts": 0,
        "generation": 1, "admitted_at_ns": int(now * 1e9), "last_event_at": now, "salvaged": False,
    }
    record.update(extra)
    return record


def salvaged(pane: str, *, now: float, generation: int = 1_800_000_000) -> dict:
    return session(pane, "Idle", now=now, agent="Herdr", delivery_status="salvaged", salvaged=True,
                   generation=generation)


def seed(cache_mgr, sessions: dict, **root) -> None:
    with cache_mgr as data:
        data["sessions"].update(sessions)
        data.update(root)
        cache_mgr.save(data)


def read_cache(cache_mgr) -> dict:
    with cache_mgr as data:
        return json.loads(json.dumps(data))


def pane_file(state_dir, pane: str, suffix: str = ""):
    return state_dir / "panes" / f"{get_hex_pane_id(pane)}{suffix}"


class LoopRunner:
    """Run ``run_reconcile_background`` under an installed FakeClock.

    ``stop()`` is checked after every pass; once it is true the run is ended by touching
    DISABLED (the loop's own exit condition). More than ``max_passes`` passes fails the
    test instead of hanging it. ``passes`` records (fake wall time, "sweep"|"heartbeat").
    """

    def __init__(self, state_dir, stop: Optional[Callable[[], bool]] = None, max_passes: int = 200) -> None:
        self.state_dir = state_dir
        self.stop = stop or (lambda: False)
        self.max_passes = max_passes
        self.passes: List[tuple] = []

    def _wrap(self, kind: str, real):
        def wrapped(*args):
            self.passes.append((clock.time(), kind))
            if len(self.passes) > self.max_passes:
                raise AssertionError(f"reconciler ran more than {self.max_passes} passes")
            result = real(*args)
            if self.stop():
                (self.state_dir / "DISABLED").touch()
            return result
        return wrapped

    def run(self, bridge_url: Optional[str] = None) -> List[tuple]:
        with mock.patch.object(background, "_sweep_pass", self._wrap("sweep", background._sweep_pass)), \
                mock.patch.object(background, "_heartbeat_pass",
                                  self._wrap("heartbeat", background._heartbeat_pass)):
            background.run_reconcile_background(bridge_url=bridge_url)
        return self.passes

    def sweep_times(self) -> List[float]:
        return [t for t, kind in self.passes if kind == "sweep"]
