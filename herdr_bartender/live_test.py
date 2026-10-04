"""--test / interactive live check against a running Bartender bridge."""

from __future__ import annotations

import sys

from . import clock
from .bridge import check_bridge_health, post_bartender_event
from .config import get_bridge_url, get_sanitized_hostname


def run_live_test():
    print(f"Checking Bartender bridge at {get_bridge_url()}...")
    health = check_bridge_health()
    if not health:
        print("[-] Error: Bartender bridge not reachable. Make sure Bartender 6 is running with Top Shelf enabled.")
        sys.exit(1)
    print(f"[+] Bartender bridge is healthy: {health}")

    host = get_sanitized_hostname()
    test_session = f"herdr:{host}:test_workspace:test_pane_1"
    print("\n1. Emitting 'Working' state...")
    post_bartender_event({
        "state": "Working",
        "agent": "Claude (Herdr)",
        "session_id": test_session,
        "title": "Unit test running in Herdr",
        "terminal": "Herdr",
    })
    print("   Sent Working. Top Shelf should show active spinner.")

    clock.sleep(1.0)

    print("\n2. Emitting 'Waiting' state (Awaiting input / attention)...")
    post_bartender_event({
        "state": "Waiting",
        "agent": "Claude (Herdr)",
        "session_id": test_session,
        "title": "Awaiting user confirmation in Herdr",
        "terminal": "Herdr",
    })
    print("   Sent Waiting. Top Shelf NotchBar should highlight attention banner.")

    clock.sleep(1.0)

    print("\n3. Emitting 'Done' state...")
    post_bartender_event({
        "state": "Done",
        "agent": "Claude (Herdr)",
        "session_id": test_session,
        "title": "Task completed successfully",
        "terminal": "Herdr",
    })
    print("   Sent Done. Top Shelf should show completion.")

    clock.sleep(1.0)

    print("\n4. Emitting 'Ended' state (Pane closed)...")
    post_bartender_event({
        "state": "Ended",
        "agent": "Claude (Herdr)",
        "session_id": test_session,
    })
    print("   Sent Ended. Session removed from Top Shelf.")

    final_health = check_bridge_health()
    print(f"\n[+] Live test complete! Final bridge health: {final_health}")
