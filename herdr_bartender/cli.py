"""Command-line entry point (bin/herdr-bartender is a thin launcher for main())."""

from __future__ import annotations

import faulthandler
import json
import os
import sys
from typing import Optional, Tuple

from . import handoff, intake, process, runtime
from .background import run_reconcile_background
from .bridge import check_bridge_health
from .cache import BoundedSessionCache, CacheError
from .cleanup import run_cleanup
from .handlers import (
    handle_agent_status_changed,
    handle_pane_closed,
    handle_tab_closed,
    handle_workspace_closed,
)
from .hooks import install_hooks, uninstall_hooks, verify_vendor_hooks_intact
from .live_test import run_live_test
from .log import log_debug
from .markers import is_disabled, touch_heartbeat
from .replay import run_replay_orphans
from .paths import get_state_dir, repo_root
from .watchdog import arm_watchdog, run_bounded


EVENT_HANDLERS = {
    intake.STATUS_EVENT: lambda data, context: handle_agent_status_changed(
        data, context, arrival_time=runtime.PROCESS_ARRIVAL_TIME, arrival_ns=runtime.PROCESS_ARRIVAL_TIME_NS),
    intake.PANE_CLOSED: lambda data, context: handle_pane_closed(
        data, context, arrival_ns=runtime.PROCESS_ARRIVAL_TIME_NS),
    intake.TAB_CLOSED: lambda data, context: handle_tab_closed(
        data, context, arrival_ns=runtime.PROCESS_ARRIVAL_TIME_NS),
    intake.WORKSPACE_CLOSED: lambda data, context: handle_workspace_closed(
        data, context, arrival_ns=runtime.PROCESS_ARRIVAL_TIME_NS),
}

STDIN_BUDGET_CAP = 0.3  # seconds; Herdr writes the envelope and closes stdin immediately
STDIN_BUDGET_SHARE = 0.25  # never spend more than this share of the remaining process budget


def _stdin_budget() -> float:
    return min(STDIN_BUDGET_CAP, runtime.time_remaining() * STDIN_BUDGET_SHARE)


def dispatch_event(event_name: str, data: dict, context: dict) -> None:
    EVENT_HANDLERS[event_name](data, context)


def run_event(argv, stdin_bytes: bytes, env) -> int:
    """R20: stdin envelope first, then argv[1], then legacy env vars. Returns an exit code."""
    event_name, data, context = intake.parse_invocation(argv, stdin_bytes, env)
    if event_name not in EVENT_HANDLERS:
        if event_name:
            log_debug(f"Ignoring unsupported event {intake.loggable(event_name)}")
        return 0
    if data is None:
        log_debug(f"Event invocation with no usable payload for {event_name!r}; safe no-op, ensuring reconciler")
        handoff.ensure_reconciler_running()
        return 0
    dispatch_event(event_name, data, context)
    return 0


FOREGROUND_FLAG = handoff.FOREGROUND_FLAG  # internal: the detached child that actually runs the reconciler loop


def _spawn_detached_reconciler() -> int:
    """R21: start the reconciler loop in its own session with no inherited stdio, then return at once.

    The process is created by the injectable ``handoff`` spawner (production: a detached ``Popen``).
    """
    if not handoff.get_spawner().spawn(handoff.loop_argv()):
        log_debug("Failed to spawn detached reconciler")
        return 1
    return 0


def run_reconcile_command(args) -> int:
    """R21: a plain --reconcile-background (the startup hook) detaches and exits; the
    --foreground child runs the singleton loop. The child never respawns, so the two
    paths cannot recurse. Returns an exit code."""
    if is_disabled():
        return 0
    if FOREGROUND_FLAG in args:
        run_reconcile_background()
        return 0
    return _spawn_detached_reconciler()


UNIT_TEST_HANG_SECONDS = 900.0   # the suite runs in about 70s: a run this long is hung


def run_unit_tests() -> int:
    """Run the stdlib unittest suite under <repo>/tests; returns a process exit code.

    A faulthandler watchdog turns a hung run into a failure that names the culprit: after
    ``UNIT_TEST_HANG_SECONDS`` every thread's stack is dumped to stderr and the process exits 1.
    """
    import unittest

    root = repo_root()
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        print(f"[-] Test directory not found: {tests_dir}", file=sys.stderr)
        return 2
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    suite = unittest.defaultTestLoader.discover(str(tests_dir), top_level_dir=str(root))
    faulthandler.dump_traceback_later(UNIT_TEST_HANG_SECONDS, exit=True)
    try:
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    finally:
        faulthandler.cancel_dump_traceback_later()
    return 0 if result.wasSuccessful() else 1


def _read_cache_or_exit(state_dir) -> dict:
    """Snapshot the cache under its lock for read-only commands; exit 1 with a message when unavailable."""
    try:
        with BoundedSessionCache(state_dir) as data:
            return data
    except CacheError as exc:
        print(f"[-] Session cache unavailable: {exc}", file=sys.stderr)
        sys.exit(1)


def main(launched_at: Optional[Tuple[float, int]] = None):
    """``launched_at``: the launcher's pre-import (monotonic, wall ns) baseline (see runtime.mark_process_start)."""
    runtime.mark_process_start(launched_at)
    args = sys.argv[1:]

    if "--unit-test" in args:
        sys.exit(run_unit_tests())

    if "--test" in args:
        run_live_test()
        return

    if "--health" in args:
        h = check_bridge_health()
        result = h or {"error": "unreachable"}
        hooks_ok, missing = verify_vendor_hooks_intact()
        result["hooks_guard_intact"] = hooks_ok
        if missing:
            result["hooks_guard_missing"] = missing
        print(json.dumps(result, indent=2))
        return

    if "--status" in args:
        state_dir = get_state_dir()
        if (state_dir / "HOOK_NEEDS_REVIEW").exists():
            print("[WARNING] Vendor hook modified upstream (SHA mismatch). Run 'herdr-bartender --install-hooks' to re-verify and approve changes.")
        data = _read_cache_or_exit(state_dir)
        sessions = data.get("sessions", {})
        print(f"Active sessions: {len(sessions)}")
        for sid, s in sessions.items():
            print(f"  {sid}: state={s.get('desired_state')} status={s.get('delivery_status')}")
        return

    if "--install-hooks" in args:
        ok = install_hooks()
        sys.exit(0 if ok else 1)

    if "--uninstall-hooks" in args:
        ok = uninstall_hooks()
        sys.exit(0 if ok else 1)

    if "--cleanup" in args:
        code = run_cleanup()
        sys.exit(code)

    if "--replay-orphans" in args:
        idx = args.index("--replay-orphans")
        if idx + 1 < len(args):
            orphan_file = args[idx + 1]
            ok = run_replay_orphans(orphan_file)
            sys.exit(0 if ok else 1)
        else:
            print("Usage: --replay-orphans <path-to-orphan-file>")
            sys.exit(1)

    if "--sessions" in args:
        print(json.dumps(_read_cache_or_exit(get_state_dir()), indent=2))
        return

    if "--reconcile-background" in args:
        sys.exit(run_reconcile_command(args))

    if is_disabled():
        sys.exit(0)

    # Normal Herdr event dispatch.
    # Resolve our own identity (a `ps` call) now: before the watchdog is armed
    # and never inside a cache-lock critical section.
    process.warm_process_identity()
    arm_watchdog()
    sys.exit(run_bounded(_run_event_path, args))


def _run_event_path(args) -> int:
    """The watchdog-bounded event path (Plan §6.1): a SIGALRM here unwinds to run_bounded()."""
    touch_heartbeat()
    stdin_bytes = intake.read_stdin_bounded(sys.stdin, budget=_stdin_budget())
    return run_event(args, stdin_bytes, os.environ)
