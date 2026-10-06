"""pane.agent_status_changed handler (Plan §2.1-§2.3, §4.1, §4.3).

Intake (``intake.resolve_identity`` / ``classify_status``) decides whether the event can
concern a session at all; Step A then stages it with ``staging.stage_status`` (tombstone
and agent-exit gates, 256-cap pruning, admission, source staleness with 0.1s tolerance,
generation and R4 ``last_event_ns`` rules) and the Universal Sender
(``handlers.flow.run_event``) claims, sends, settles and dispatches it.
"""

from __future__ import annotations

from typing import Optional

from .. import clock
from ..intake import STATUS_EVENT, Identity, classify_status, loggable, resolve_identity
from ..log import log_debug
from ..markers import touch_heartbeat
from ..process import memoised_herdr_alive
from ..sender import Stage, Staged, Target
from ..staging import stage_status
from .flow import run_event


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _status_stage(identity: Identity, event_data: dict, context: dict, arr_ns: int, arr_time: float,
                  spool_generation: Optional[int]) -> Stage:
    def stage(data: dict) -> Staged:
        staged = stage_status(data, identity, event_data, context, arr_ns, arr_time,
                              herdr_alive=memoised_herdr_alive, spool_generation=spool_generation,
                              can_export_orphans=True)
        if not staged.staged:
            return Staged()
        target = Target(staged.session_id, identity.canonical_pane, staged.record)
        return Staged((target,), mutated=True, orphan_exports=staged.orphan_exports)
    return stage


def handle_agent_status_changed(event_data: dict, context: dict, bridge_url: Optional[str] = None,
                                arrival_time: Optional[float] = None, arrival_ns: Optional[int] = None,
                                spool_generation: Optional[int] = None) -> None:
    arr_time = arrival_time or clock.time()
    arr_ns = arrival_ns or clock.time_ns()
    event_data, context = _as_dict(event_data), _as_dict(context)

    def prepare() -> Optional[Stage]:
        touch_heartbeat()
        identity, notes = resolve_identity(event_data, context)
        for note in notes:
            log_debug(note)
        if identity is None:
            return None
        agent_status = event_data.get("agent_status")
        if classify_status(agent_status) == "unrecognized":
            log_debug(f"Warning: unrecognized agent_status {loggable(agent_status)} for pane {identity.canonical_pane}")
            return None
        return _status_stage(identity, event_data, context, arr_ns, arr_time, spool_generation)

    run_event(STATUS_EVENT, event_data, context, arr_ns, prepare, bridge_url)
