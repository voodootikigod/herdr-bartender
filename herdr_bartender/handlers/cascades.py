"""tab.closed / workspace.closed container cascades (Plan §4.3 item 5, §5.2).

ONE Step A transaction: DISABLED re-checked under the lock, every matching session
staged to Ended and its pane tombstoned (whoever holds its lease), at most one lease
claimed (``EVENT_POLICY.max_sessions``), the overflow sessions' leases cleared and left
``in_flight`` in the same save. The reconciler is flagged once, after the lock is
released; the single inline Ended goes through the Universal Sender (budget-gated, and a
retryable failure hands off to the reconciler).
"""

from __future__ import annotations

from typing import Optional

from .. import clock
from ..intake import TAB_CLOSED, WORKSPACE_CLOSED, container_id, loggable
from ..log import log_debug
from ..markers import touch_heartbeat
from ..sender import Stage, Staged, Target
from ..staging import stage_container_close
from .flow import run_event


def _container_stage(event_name: str, event_data: dict, arr_ns: int, spool_generation: Optional[int]) -> Stage:
    def stage(data: dict) -> Staged:
        targets = stage_container_close(data, event_name, event_data, arr_ns, spool_generation)
        return Staged(tuple(Target(t.session_id, t.pane_id, t.record) for t in targets), mutated=bool(targets))
    return stage


def _handle_container_close(event_name: str, id_key: str, event_data, context, bridge_url: Optional[str],
                            arrival_ns: Optional[int], spool_generation: Optional[int]) -> None:
    arr_ns = arrival_ns or clock.time_ns()
    event_data = event_data if isinstance(event_data, dict) else {}

    def prepare() -> Optional[Stage]:
        touch_heartbeat()
        # Exact container ids from event data only; no focused-context fallback (§2.1, gap cascade-container-matching).
        if not container_id(event_data, id_key):
            log_debug(f"Ignoring {event_name} without a valid {id_key}: {loggable(event_data.get(id_key))}")
            return None
        return _container_stage(event_name, event_data, arr_ns, spool_generation)

    run_event(event_name, event_data, context if isinstance(context, dict) else {}, arr_ns, prepare, bridge_url)


def handle_tab_closed(event_data: dict, context: dict, bridge_url: Optional[str] = None,
                      arrival_ns: Optional[int] = None, spool_generation: Optional[int] = None) -> None:
    _handle_container_close(TAB_CLOSED, "tab_id", event_data, context, bridge_url, arrival_ns, spool_generation)


def handle_workspace_closed(event_data: dict, context: dict, bridge_url: Optional[str] = None,
                            arrival_ns: Optional[int] = None, spool_generation: Optional[int] = None) -> None:
    _handle_container_close(WORKSPACE_CLOSED, "workspace_id", event_data, context, bridge_url, arrival_ns,
                            spool_generation)
