"""pane.closed handler (Plan §2.1, §4.1 tombstones, §4.3; Step A staging in ``staging.stage_pane_close``).

Step A ends every matching session (close origin persisted on the session, R7),
tombstones the pane only when the close is not stale against the admission (R8), and
queues the pane's vendor dismissal under the same lock; the Universal Sender delivers at
most one Ended inline and hands any remainder to the reconciler.
"""

from __future__ import annotations

from typing import Optional

from .. import clock
from ..intake import PANE_CLOSED, resolve_identity
from ..log import log_debug
from ..markers import touch_heartbeat
from ..sender import Stage, Staged, Target
from ..staging import stage_pane_close
from .flow import run_event


def _pane_stage(pane: str, event_data: dict, arr_ns: int, spool_generation: Optional[int]) -> Stage:
    def stage(data: dict) -> Staged:
        targets = stage_pane_close(data, pane, event_data, arr_ns, spool_generation)
        return Staged(tuple(Target(t.session_id, t.pane_id, t.record) for t in targets), mutated=True)
    return stage


def handle_pane_closed(event_data: dict, context: dict, bridge_url: Optional[str] = None,
                       arrival_ns: Optional[int] = None, spool_generation: Optional[int] = None) -> None:
    arr_ns = arrival_ns or clock.time_ns()
    event_data = event_data if isinstance(event_data, dict) else {}

    def prepare() -> Optional[Stage]:
        touch_heartbeat()
        # Identity from event data only: never the focused pane or context.workspace_id (§2.3, R1/R2).
        identity, notes = resolve_identity(event_data, {})
        for note in notes:
            log_debug(note)
        if identity is None:
            return None
        return _pane_stage(identity.canonical_pane, event_data, arr_ns, spool_generation)

    run_event(PANE_CLOSED, event_data, context if isinstance(context, dict) else {}, arr_ns, prepare, bridge_url)
