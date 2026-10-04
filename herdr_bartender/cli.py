"""Command-line entry point (bin/herdr-bartender is a thin launcher for main())."""

from __future__ import annotations

import json
import os
import sys

from . import runtime
from .background import run_reconcile_background
from .bridge import check_bridge_health
from .cache import BoundedSessionCache
from .cleanup import run_cleanup
from .handlers import (
    handle_agent_status_changed,
    handle_pane_closed,
    handle_tab_closed,
    handle_workspace_closed,
)
from .hooks import install_hooks, uninstall_hooks, verify_vendor_hooks_intact
from .live_test import run_live_test
from .markers import is_disabled, touch_heartbeat
from .orphans import run_replay_orphans
from .paths import get_state_dir, repo_root
from .process import own_start_time
from .watchdog import arm_watchdog


def run_unit_tests() -> int:
    """Run the stdlib unittest suite under <repo>/tests; returns a process exit code."""
    import unittest

    root = repo_root()
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        print(f"[-] Test directory not found: {tests_dir}", file=sys.stderr)
        return 2
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    suite = unittest.defaultTestLoader.discover(str(tests_dir), top_level_dir=str(root))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main():
    runtime.mark_process_start()
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
        cache_mgr = BoundedSessionCache(state_dir)
        with cache_mgr as data:
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
        cache_mgr = BoundedSessionCache(get_state_dir())
        with cache_mgr as data:
            print(json.dumps(data, indent=2))
        return

    if "--reconcile-background" in args:
        run_reconcile_background()
        return

    if is_disabled():
        sys.exit(0)

    # Normal Herdr event dispatch
    # Resolve our own start time (a `ps` call) now, as the original did at load:
    # before the watchdog is armed and never inside a cache-lock critical section.
    own_start_time()
    arm_watchdog()
    touch_heartbeat()
    event_name = os.environ.get("HERDR_PLUGIN_EVENT", "")
    event_json_raw = os.environ.get("HERDR_PLUGIN_EVENT_JSON", "{}")
    context_json_raw = os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", "{}")

    try:
        envelope = json.loads(event_json_raw)
    except Exception:
        envelope = {}

    try:
        context = json.loads(context_json_raw)
    except Exception:
        context = {}

    data = envelope.get("data", {})

    if event_name == "pane.agent_status_changed":
        handle_agent_status_changed(data, context, arrival_time=runtime.PROCESS_ARRIVAL_TIME, arrival_ns=runtime.PROCESS_ARRIVAL_TIME_NS)
    elif event_name == "pane.closed":
        handle_pane_closed(data, context, arrival_ns=runtime.PROCESS_ARRIVAL_TIME_NS)
    elif event_name == "tab.closed":
        handle_tab_closed(data, context, arrival_ns=runtime.PROCESS_ARRIVAL_TIME_NS)
    elif event_name == "workspace.closed":
        handle_workspace_closed(data, context, arrival_ns=runtime.PROCESS_ARRIVAL_TIME_NS)
