"""Pure Step A staging: cache mutations for one event, with no lease, no I/O and no network.

Plan §4.3 Step A "Live Event Evaluation" and the spool replay rules. Handlers call
these under the cache lock and then do their own lease evaluation; spool replay
(``spool.replay_spool_locked``) calls them for each envelope and leaves delivery to
the reconciler.

``stage_status`` decides first and mutates last: a dropped event leaves ``data``
untouched, so several envelopes can be staged into one batch and saved once.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Tuple

from . import clock
from .cache import ORPHAN_MIRROR_OWED, SESSION_CAP, event_time, mirror_copy, safe_to_evict
from .delivery_state import foreign_lease_open, record_tombstone, stage_vendor_cleanup
from .intake import (
    PANE_CLOSED,
    TAB_CLOSED,
    WORKSPACE_CLOSED,
    Identity,
    admit_status,
    build_close_payload,
    build_session_id,
    classify_status,
    container_id,
    loggable,
    resolve_fields,
    resolve_host,
    session_matches_pane,
    session_matches_tab,
    session_matches_workspace,
)
from .log import log_debug, log_warning
from .sanitize import format_agent_name

TOMBSTONE_WINDOW_NS = 60_000_000_000
AGENT_EXIT_TTL_NS = 60_000_000_000
SOURCE_STALENESS_TOLERANCE = 0.1   # Plan §4.3 L425: drop only when older than last_source_timestamp - 0.1s

STAGED = "staged"
CLOSE_ORIGIN_FIELDS = ("close_kind", "closed_at_ns", "closed_source_ts", "exit_at_ns", "exit_source_ts")
# Lifecycle stamps of the previous turn's Ended (TTL expiry, orphan horizon, R12): a new turn starts without them.
PREVIOUS_TURN_FIELDS = CLOSE_ORIGIN_FIELDS + ("ttl_expired_at", "expiry_reason", "orphaned_ended", "orphaned_at",
                                              ORPHAN_MIRROR_OWED)
# A salvaged record stamps these with the salvage time, which is not a real arrival or admission (Plan §6.3).
SALVAGE_STAMPED_FIELDS = ("admitted_at_ns", "last_arrival_ns", "last_event_ns", "last_applied_arrival_time")
# R48: what a later-processed close needs to judge this generation's admission by arrival order, not lock order.
ADMISSION_SIGNAL = "admission_signal"   # the generation's admitting event (+ the pane's pre-admission source ts)
POSITIVE_SIGNAL = "positive_signal"     # the generation's latest positive ``working`` event (event agent present)


@dataclass(frozen=True)
class StatusStage:
    session_id: str
    record: Optional[dict]   # the live session record when staged, else None
    reason: str
    # (sid, snapshot) of undelivered Ended records pruned at the 256 cap: Step A journals them durably
    # BEFORE the save that prunes them; the orphan file absorbs them after the lock (zero data loss, R12).
    orphan_exports: Tuple[Tuple[str, dict], ...] = ()

    @property
    def staged(self) -> bool:
        return self.record is not None


@dataclass(frozen=True)
class _Gate:
    reason: Optional[str] = None          # drop reason, or None to continue
    pop_agent_exit: bool = False
    evictions: Tuple[str, ...] = ()        # Ended/salvaged records pruned to admit a new pane at the cap
    warn: bool = False                     # log the drop as a WARNING (Plan §4.1 L109: 256-cap refusal)


@dataclass(frozen=True)
class CloseTarget:
    session_id: str
    pane_id: Optional[str]
    record: dict
    payload: dict
    seq: int


def _num(value: object) -> Optional[float]:
    """A finite float, or None (absent, not a number, ``"NaN"``/``"inf"``, or an int beyond the float range)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _drop(sid: str, reason: str) -> StatusStage:
    log_debug(reason)
    return StatusStage(sid, None, reason)


def _ordering_view(cached: Mapping) -> Mapping:
    """``cached`` as seen by the arrival-ordering checks.

    Salvage cannot know when a session was admitted or last heard from, so a
    salvaged record exposes no arrival stamps: pre-crash closes and agent exits
    preserved in spool/ (Plan §6.3 step 2) still apply to it.
    """
    if not cached.get("salvaged"):
        return cached
    return {key: value for key, value in cached.items() if key not in SALVAGE_STAMPED_FIELDS}


def _prunable_at_cap(record: Mapping, allow_undelivered: bool, now_wall: float) -> bool:
    """Plan §4.1 L292: only Ended and salvaged records make room; live sessions never do.

    An undelivered Ended is prunable only when the caller can export it to the orphan file
    (``allow_undelivered``); otherwise only the save-time safe set (``cache.safe_to_evict``) is.
    A record another sender is delivering right now (open foreign lease) is never pruned.
    """
    if foreign_lease_open(record, now_wall):
        return False
    if allow_undelivered:
        return bool(record.get("salvaged")) or record.get("desired_state") == "Ended"
    return safe_to_evict(dict(record))


def _capacity_evictions(sessions: Mapping, sid: str, allow_undelivered: bool,
                        now_wall: float) -> Optional[Tuple[str, ...]]:
    """Plan §4.3 capacity check for a new pane at the 256 cap: the Ended/salvaged records to prune first
    (oldest ``last_event_at`` first), or None when only live sessions remain (reject)."""
    if sid in sessions or len(sessions) < SESSION_CAP:
        return ()
    oldest_first = sorted(sessions.items(), key=lambda kv: event_time(kv[1]))
    prunable = [key for key, rec in oldest_first if _prunable_at_cap(rec, allow_undelivered, now_wall)]
    needed = len(sessions) - (SESSION_CAP - 1)
    return tuple(prunable[:needed]) if len(prunable) >= needed else None


def _undelivered_ended(record: Mapping) -> bool:
    return record.get("desired_state") == "Ended" and not safe_to_evict(dict(record))


# -- status --------------------------------------------------------------------------
def _positive_agent(event_data: Mapping) -> bool:
    """Plan §4.3 L419: ``event.data.agent`` is non-empty (the raw value; the focused context never counts)."""
    raw_agent = event_data.get("agent")
    return bool(raw_agent) and bool(str(raw_agent).strip())


def _tombstone_verdict(entry: object, agent_status: object, src_ts: Optional[float], has_agent: bool, arr_ns: int,
                       herdr_alive: Callable[[], bool], pane: str) -> Optional[str]:
    """None: no tombstone or it may be popped; otherwise the drop reason (Plan §4.3 tombstone checks)."""
    if isinstance(entry, dict):
        closed_ns = int(entry.get("closed_at_ns", 0) or 0)
        closed_src = _num(entry.get("closed_source_ts")) or 0.0
        last_src = _num(entry.get("last_source_timestamp")) or 0.0
    else:
        closed_ns, closed_src, last_src = int(entry or 0), 0.0, 0.0
    if arr_ns <= closed_ns:
        return f"Rejecting late event for closed pane {pane}: arrival {arr_ns} <= tombstone {closed_ns}"
    if src_ts is not None and last_src and src_ts <= last_src:
        return f"Rejecting trailing event for closed pane {pane}: source ts {src_ts} <= tombstone ts {last_src}"
    if arr_ns - closed_ns >= TOMBSTONE_WINDOW_NS:
        return None
    if agent_status != "working":
        return f"Rejecting non-working status {loggable(agent_status)} for recently closed pane {pane}"
    if not has_agent or not herdr_alive():
        return f"Rejecting event without positive admission signal for recently closed pane {pane}"
    if src_ts is not None and (src_ts <= closed_src or src_ts <= last_src):
        return f"Rejecting pre-close status with source ts {src_ts} <= closed_source_ts {closed_src}"
    return None


def _agent_exit_verdict(entry: object, agent_status: object, src_ts: Optional[float], arr_ns: int,
                        pane: str, has_event_agent: bool) -> Tuple[Optional[str], bool]:
    """(drop reason or None, pop the agent_exits entry).

    Plan §4.3 L424: only a fresh ``working`` event carrying ``event.data.agent`` clears the entry.
    """
    if entry is None:
        return None, False
    if not isinstance(entry, dict):
        return None, True
    exit_ns = int(entry.get("exit_at_ns", 0) or 0)
    exit_src = _num(entry.get("exit_source_ts")) or 0.0
    if arr_ns - exit_ns > AGENT_EXIT_TTL_NS:
        return None, True
    if arr_ns <= exit_ns or (src_ts is not None and src_ts <= exit_src):
        return f"Rejecting stale event arriving after agent exit for pane {pane}", False
    return None, agent_status == "working" and has_event_agent


def _superseded_replay(cached: Mapping, src_ts: Optional[float], arr_ns: int) -> bool:
    """Plan §4.3 spool replay: source ts vs last_source_timestamp, else arrival vs last_event_ns."""
    if not cached:
        return False
    if src_ts is not None:
        return src_ts < (_num(cached.get("last_source_timestamp")) or 0.0)
    return arr_ns < int(cached.get("last_event_ns", 0) or 0)


def _clamped_event_ns(arr_ns: int, cached: Mapping) -> int:
    """R4: strictly monotonic ``last_event_ns`` on equal or backward arrival clocks."""
    return max(arr_ns, int(cached.get("last_event_ns", 0) or 0) + 1)


def _next_generation(data: dict, pane: str, cached: Mapping) -> int:
    return max(int(data.get("next_generation", 1) or 1), int(data.get("pane_generations", {}).get(pane, 0) or 0),
               int(cached.get("generation", 0) or 0)) + 1


def _commit_status(data: dict, sid: str, identity: Identity, event_data: Mapping, context: Mapping,
                   plan: dict) -> dict:
    """Apply a decided status event to ``data`` (the only mutating step)."""
    pane = identity.canonical_pane
    for evicted in plan["evictions"]:
        log_debug(f"Pruning {evicted} to admit {sid} at the {SESSION_CAP}-session cap")
        data["sessions"].pop(evicted, None)
    if plan["pop_tombstone"]:
        data.get("tombstones", {}).pop(pane, None)
    if plan["pop_agent_exit"]:
        data.get("agent_exits", {}).pop(pane, None)
    record = data["sessions"].setdefault(sid, {})
    event_ns = _clamped_event_ns(plan["arr_ns"], record)
    signal = plan["signal"]
    if plan["new_generation"] is not None:
        gen = plan["new_generation"]
        data["next_generation"] = gen
        data.setdefault("pane_generations", {})[pane] = gen
        record.update({"generation": gen, "admitted_at_ns": plan["arr_ns"],
                       ADMISSION_SIGNAL: {**signal, "prior_src": _num(record.get("last_source_timestamp")) or 0.0}})
        for stale in PREVIOUS_TURN_FIELDS + (POSITIVE_SIGNAL,):  # a new turn starts without the previous turn's
            record.pop(stale, None)                               # close origin and admission evidence
    if signal["status"] == "working" and signal["agent"]:
        record[POSITIVE_SIGNAL] = signal
    record["salvaged"] = False
    seq = int(record.get("seq", 0) or 0) + 1
    fields, mapped, arr_ns = plan["fields"], plan["mapped_state"], plan["arr_ns"]
    payload = {"state": mapped, "agent": plan["agent_name"], "session_id": sid, "title": fields.title,
               "cwd": fields.cwd, "terminal": "Herdr", "event": "pane.agent_status_changed", "seq": seq}
    record.update({
        "pane_id": pane, "workspace_id": fields.workspace_id, "tab_id": fields.tab_id, "host": plan["host"],
        "agent": plan["agent_name"], "raw_agent": plan["raw_agent"], "title": fields.title, "cwd": fields.cwd,
        "desired_state": mapped, "desired_payload": payload, "seq": seq, "delivery_status": "in_flight",
        "delivery_error": None, "delivery_attempts": 0, "next_retry_at": None,
        "last_applied_arrival_time": plan["arr_time"],
        "last_arrival_ns": arr_ns, "last_event_ns": event_ns, "last_event_at": plan["now_wall"],
    })
    if mapped == "Ended":
        exit_src = float(_num(event_data.get("timestamp")) or 0.0)
        record.update({"close_kind": "agent_exit", "exit_at_ns": arr_ns, "exit_source_ts": exit_src})
        data.setdefault("agent_exits", {})[pane] = {"exit_at_ns": arr_ns, "exit_source_ts": exit_src}
    if plan["source_ts"] is not None:  # never moves backward (an accepted event may be <= 0.1s older)
        previous = _num(record.get("last_source_timestamp"))
        record["last_source_timestamp"] = plan["source_ts"] if previous is None else max(previous, plan["source_ts"])
    return record


def stage_status(data: dict, identity: Identity, event_data: Mapping, context: Mapping, arr_ns: int,
                 arr_time: Optional[float] = None, *, herdr_alive: Callable[[], bool],
                 spool_generation: Optional[int] = None, require_newer: bool = False,
                 now_wall: Optional[float] = None, can_export_orphans: bool = False) -> StatusStage:
    """Stage a ``pane.agent_status_changed`` event into ``data`` (Plan §4.3 Step A) without a lease.

    ``require_newer`` adds the spool-replay supersession rule. ``can_export_orphans``: the
    caller journals ``StatusStage.orphan_exports`` before its save, so an undelivered Ended may be
    pruned at the 256 cap (without it only already-safe records make room). Returns the
    staged record, or None with the drop reason (``data`` is then unchanged).
    """
    pane = identity.canonical_pane
    host = resolve_host(data.get("host"))
    sid = build_session_id(host, pane)
    if not sid:
        return _drop("", f"Rejecting out-of-bounds session_id for host {loggable(host)} pane {pane}")
    arr_time = arr_ns / 1e9 if arr_time is None else arr_time
    now_wall = clock.time() if now_wall is None else now_wall
    cached = data["sessions"].get(sid, {})
    src_ts = _num(event_data.get("timestamp"))
    gate = _gate_reason(data, sid, pane, event_data, arr_ns, src_ts, cached, herdr_alive, require_newer,
                        can_export_orphans, now_wall)
    if gate.reason:
        if gate.warn:
            log_warning(gate.reason)
            return StatusStage(sid, None, gate.reason)
        return _drop(sid, gate.reason)
    admission = admit_status(event_data.get("agent_status"), event_data, context, identity, cached)
    if admission is None:
        return StatusStage(sid, None, "not admitted")
    reason = _ordering_reason(_ordering_view(cached), src_ts, arr_ns, arr_time)
    if reason:
        return _drop(sid, reason)
    # Plan §4.3 L441: a new admission or a desired Ended -> live transition (never a delivered Ended alone).
    new_turn = cached.get("desired_state") == "Ended" or "generation" not in cached
    new_gen = _next_generation(data, pane, cached) if new_turn else None
    effective_gen = new_gen if new_gen is not None else cached.get("generation", 1)
    if spool_generation is not None and effective_gen > spool_generation:
        return _drop(sid, f"Ignoring spooled event with older generation {spool_generation} < {effective_gen}")
    exports = tuple((evicted, mirror_copy(data["sessions"][evicted], orphaned_ended=True))
                    for evicted in gate.evictions if _undelivered_ended(data["sessions"][evicted]))
    plan = {
        "pop_tombstone": pane in data.get("tombstones", {}), "pop_agent_exit": gate.pop_agent_exit,
        "evictions": gate.evictions, "new_generation": new_gen,
        "mapped_state": admission.mapped_state, "raw_agent": admission.raw_agent,
        "agent_name": format_agent_name(admission.raw_agent),
        "fields": resolve_fields(event_data, context, identity, cached), "host": host, "arr_ns": arr_ns,
        "arr_time": arr_time, "now_wall": now_wall, "source_ts": src_ts,
        "signal": {"arr_ns": arr_ns, "status": event_data.get("agent_status"), "agent": _positive_agent(event_data),
                   "src_ts": src_ts},
    }
    return StatusStage(sid, _commit_status(data, sid, identity, event_data, context, plan), STAGED, exports)


def _has_event_agent(event_data: Mapping) -> bool:
    raw = event_data.get("agent")
    return raw is not None and bool(str(raw).strip())


def _gate_reason(data: dict, sid: str, pane: str, event_data: Mapping, arr_ns: int, src_ts: Optional[float],
                 cached: Mapping, herdr_alive: Callable[[], bool], require_newer: bool,
                 can_export_orphans: bool, now_wall: float) -> _Gate:
    """Pre-admission gates in Plan §4.3 order; decides only (evictions are applied by ``_commit_status``)."""
    agent_status = event_data.get("agent_status")
    status_kind = classify_status(agent_status)
    if status_kind == "unrecognized":
        return _Gate(f"Warning: unrecognized agent_status {loggable(agent_status)} for pane {pane}")
    if require_newer and _superseded_replay(_ordering_view(cached), src_ts, arr_ns):
        return _Gate(f"Dropping superseded spooled status for {sid}")
    tomb = data.get("tombstones", {}).get(pane)
    if tomb:
        reason = _tombstone_verdict(tomb, agent_status, src_ts, _positive_agent(event_data), arr_ns, herdr_alive, pane)
        if reason:
            return _Gate(reason)
    exit_reason, pop_exit = _agent_exit_verdict(data.get("agent_exits", {}).get(pane), agent_status, src_ts,
                                                arr_ns, pane, _has_event_agent(event_data))
    if exit_reason:
        return _Gate(exit_reason)
    evictions = _capacity_evictions(data["sessions"], sid, can_export_orphans, now_wall)
    if evictions is None:
        return _Gate(f"Cache capacity limit of {SESSION_CAP} active sessions reached; refusing {sid}", warn=True)
    if status_kind == "unknown":
        return _Gate(f"Debouncing transient unknown status for pane {pane}")
    return _Gate(None, pop_exit, evictions)


def _ordering_reason(cached: Mapping, src_ts: Optional[float], arr_ns: int, arr_time: float) -> Optional[str]:
    """Source staleness (Plan §4.3 L425, 0.1s tolerance) and arrival ordering against the cached session."""
    last_src = _num(cached.get("last_source_timestamp"))
    if src_ts is not None and last_src is not None and src_ts < last_src - SOURCE_STALENESS_TOLERANCE:
        return f"Dropping stale source timestamp {src_ts} < {last_src} - {SOURCE_STALENESS_TOLERANCE}"
    if arr_ns < cached.get("last_arrival_ns", 0) or arr_time < cached.get("last_applied_arrival_time", 0.0):
        return f"Dropping older event: arrival {arr_ns} < last_arrival {cached.get('last_arrival_ns', 0)}"
    return None


# -- closes --------------------------------------------------------------------------
def _close_session(record: dict, sid: str, event_name: str, event_data: Mapping, arr_ns: int) -> CloseTarget:
    """Stage one matched session to Ended (Plan §4.3 Step A container close).

    The close origin (closed_at_ns, closed_source_ts) is recorded once: a duplicate close of an
    already-closing session (Herdr's dual teardown: tab.closed then pane.closed) keeps it.
    """
    seq = int(record.get("seq", 0) or 0) + 1
    already_closing = record.get("desired_state") == "Ended" and record.get("close_kind") == "container" \
        and record.get("closed_at_ns")
    record["salvaged"] = False  # a real close re-activates a salvaged record so its Ended is transmitted (§6.3 rule 5)
    if not already_closing:
        closed_src = max(_num(event_data.get("timestamp")) or 0.0,
                         _num(record.get("last_source_timestamp")) or 0.0, float(arr_ns) / 1e9)
        record.update({"closed_at_ns": arr_ns, "closed_source_ts": closed_src, "close_kind": "container"})
    record.update({"desired_state": "Ended", "seq": seq, "delivery_status": "in_flight", "delivery_error": None,
                   "delivery_attempts": 0, "next_retry_at": None})
    payload = build_close_payload(sid, record, event_name, seq)
    record["desired_payload"] = payload
    return CloseTarget(sid, record.get("pane_id"), record, payload, seq)


def _admission_outlives_close(record: Mapping, arr_ns: int, event_ts: float, herdr_alive: Callable[[], bool],
                              pane: str) -> bool:
    """R48: would this generation have been admitted had the close (arrival ``arr_ns``) been processed first?

    Lock order is not arrival order: a trailing status that won the lock before an earlier-arrived close
    must be judged against the tombstone that close records (Plan §4.3 L410-421), exactly as it would have
    been had the close won. The generation survives when its admitting event, or its latest positive
    ``working`` event, passes that tombstone; otherwise the close ends it. A record admitted before
    admission signals were persisted keeps the plain "close predating admission" rule.
    """
    admission = record.get(ADMISSION_SIGNAL)
    if not isinstance(admission, dict):
        return True
    prior_src = _num(admission.get("prior_src")) or 0.0
    tombstone = {"closed_at_ns": arr_ns, "closed_source_ts": max(event_ts, prior_src, float(arr_ns) / 1e9),
                 "last_source_timestamp": max(prior_src, event_ts)}
    for signal in (admission, record.get(POSITIVE_SIGNAL)):
        if not isinstance(signal, dict):
            continue
        reason = _tombstone_verdict(tombstone, signal.get("status"), _num(signal.get("src_ts")),
                                    bool(signal.get("agent")), int(signal.get("arr_ns") or 0), herdr_alive, pane)
        if reason is None:
            return True
        log_debug(f"Close at {arr_ns} ends a later admission its tombstone would have rejected ({reason})")
    return False


def _eligible(record: Mapping, arr_ns: int, spool_generation: Optional[int], event_ts: float = 0.0,
              herdr_alive: Callable[[], bool] = lambda: True) -> bool:
    """R8: skip only when session.generation > envelope.generation; skip closes predating a real admission.

    A salvaged record's ``admitted_at_ns`` is the salvage time, not an admission, so the
    pre-crash closes salvage kept in spool/ still end it (Plan §6.3 step 2). An admission
    after the close that the close's tombstone would have rejected does not protect the
    session (R48: the outcome follows arrival order, not which process won the lock).
    """
    if spool_generation is not None and record.get("generation", 1) > spool_generation:
        log_debug(f"Ignoring spooled close with older generation {spool_generation} < {record.get('generation')}")
        return False
    if _ordering_view(record).get("admitted_at_ns", 0) > arr_ns:
        if _admission_outlives_close(record, arr_ns, event_ts, herdr_alive, str(record.get("pane_id") or "")):
            log_debug(f"Ignoring close predating session admission: close {arr_ns} < admitted {record.get('admitted_at_ns')}")
            return False
    return True


@dataclass(frozen=True)
class PaneCloseStage:
    targets: Tuple[CloseTarget, ...]
    recorded: bool   # the close was applied: pane tombstoned, vendor cleanup staged, marker to be removed


def stage_pane_close_result(data: dict, canonical_pane: str, event_data: Mapping, arr_ns: int,
                            spool_generation: Optional[int] = None, *,
                            herdr_alive: Callable[[], bool] = lambda: True) -> PaneCloseStage:
    """``pane.closed``: stage every matching session to Ended and tombstone the pane.

    The tombstone is recorded only when a matching session accepted the close, or no
    session is cached for the pane; a close predating a real admission of the session
    changes nothing (gap tombstone-on-stale-close, R48). ``recorded`` tells the caller to
    remove the pane marker under the same lock (Plan §1 L59).
    """
    sessions = data["sessions"]
    host = resolve_host(data.get("host"))
    matching = [(sid, rec) for sid, rec in sessions.items() if session_matches_pane(sid, rec, canonical_pane, host)]
    last_src = max([_num(rec.get("last_source_timestamp")) or 0.0 for _, rec in matching] + [0.0])
    event_ts = _num(event_data.get("timestamp")) or 0.0
    targets = []
    for sid, rec in matching:
        if _eligible(rec, arr_ns, spool_generation, event_ts, herdr_alive):
            target = _close_session(rec, sid, PANE_CLOSED, event_data, arr_ns)
            targets.append(CloseTarget(sid, canonical_pane, rec, target.payload, target.seq))
    recorded = bool(targets) or not matching
    if recorded:
        record_tombstone(data, canonical_pane, arr_ns, max(event_ts, last_src, float(arr_ns) / 1e9),
                         max(last_src, event_ts))
        # Plan §1 L57 / §3.2: a pane close dismisses any vendor entry recorded for the pane, whatever
        # happens to the session's own Ended (persisted; the event path resolves it in the same lock hold).
        stage_vendor_cleanup(data, canonical_pane, True, clock.time())
    return PaneCloseStage(tuple(targets), recorded)


def stage_pane_close(data: dict, canonical_pane: str, event_data: Mapping, arr_ns: int,
                     spool_generation: Optional[int] = None, *,
                     herdr_alive: Callable[[], bool] = lambda: True) -> Tuple[CloseTarget, ...]:
    """The targets of ``stage_pane_close_result`` (callers that leave the marker to the Ended's Step C)."""
    return stage_pane_close_result(data, canonical_pane, event_data, arr_ns, spool_generation,
                                   herdr_alive=herdr_alive).targets


def container_matcher(event_name: str, event_data: Mapping) -> Optional[Callable[[Mapping], bool]]:
    """The exact container predicate for a cascade event, or None when the container id is invalid."""
    if event_name == TAB_CLOSED:
        tab = container_id(event_data, "tab_id")
        closed_ws = container_id(event_data, "workspace_id")
        return (lambda rec: session_matches_tab(rec, tab, closed_ws)) if tab else None
    if event_name == WORKSPACE_CLOSED:
        ws = container_id(event_data, "workspace_id")
        return (lambda rec: session_matches_workspace(rec, ws)) if ws else None
    raise ValueError(f"not a container event: {event_name!r}")


def stage_container_close(data: dict, event_name: str, event_data: Mapping, arr_ns: int,
                          spool_generation: Optional[int] = None, *,
                          herdr_alive: Callable[[], bool] = lambda: True) -> Tuple[CloseTarget, ...]:
    """``tab.closed`` / ``workspace.closed``: stage matching sessions to Ended and tombstone each pane."""
    matches = container_matcher(event_name, event_data)
    if matches is None:
        log_debug(f"Ignoring {event_name} without a valid container id")
        return ()
    event_ts = _num(event_data.get("timestamp")) or 0.0
    targets = []
    for sid, rec in list(data["sessions"].items()):
        if not matches(rec) or not _eligible(rec, arr_ns, spool_generation, event_ts, herdr_alive):
            continue
        target = _close_session(rec, sid, event_name, event_data, arr_ns)
        if target.pane_id:
            record_tombstone(data, target.pane_id, arr_ns, rec["closed_source_ts"],
                             max(_num(rec.get("last_source_timestamp")) or 0.0, _num(event_data.get("timestamp")) or 0.0))
        targets.append(target)
    return tuple(targets)
