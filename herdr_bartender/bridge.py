"""HTTP client for the Bartender NotchBar bridge."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from . import runtime
from .config import get_bridge_url
from .log import log_debug
from .process import get_bartender_pid


def _raw_post_event(payload: dict, timeout: float = 0.2, bridge_url: str | None = None) -> tuple[bool, bool]:
    if runtime.IN_CRITICAL_SECTION:
        log_debug("FATAL: Network I/O attempted while holding critical section file lock!")
        if os.environ.get("HERDR_BARTENDER_UNIT_TESTING"):
            raise AssertionError("Network I/O attempted while holding critical section file lock!")
    # Gate transmission on Bartender process liveness to prevent leaking data to unrelated listeners
    if bridge_url is None and get_bartender_pid() is None:
        log_debug("Refusing to post to unverified loopback listener: Bartender process not running")
        return (False, False)
    tr = runtime.time_remaining()
    eff_timeout = max(0.05, min(timeout, tr - 0.3 if tr > 0.4 else tr))
    url = f"{bridge_url or get_bridge_url()}/event"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=eff_timeout) as resp:
            if resp.status == 200:
                try:
                    res_body = json.loads(resp.read().decode("utf-8", errors="ignore"))
                    if res_body.get("ok") is True:
                        return (True, False)
                    return (False, True)
                except Exception:
                    return (True, False)
            elif 300 <= resp.status < 500:
                return (False, True)
            else:
                return (False, False)
    except urllib.error.HTTPError as e:
        log_debug(f"HTTPError {e.code} posting to {url}: {e}")
        if 300 <= e.code < 500:
            return (False, True)
        return (False, False)
    except Exception as e:
        log_debug(f"Failed to post event to {url}: {e}")
        return (False, False)


def post_bartender_event(payload: dict, timeout: float = 0.2, bridge_url: str | None = None) -> tuple[bool, bool]:
    """Posts event to Bartender Pro bridge. Retries Ended with minimal payload on failure."""
    succ, is_non_ret = _raw_post_event(payload, timeout=timeout, bridge_url=bridge_url)
    if not succ and payload.get("state") == "Ended":
        min_payload = {
            "state": "Ended",
            "agent": payload.get("agent", "Herdr"),
            "session_id": payload.get("session_id"),
        }
        if min_payload != payload:
            log_debug("Retrying Ended event with minimal payload")
            succ, is_non_ret = _raw_post_event(min_payload, timeout=timeout, bridge_url=bridge_url)
    return (succ, is_non_ret)


def check_bridge_health(timeout: float = 0.6, bridge_url: str | None = None) -> dict | None:
    if bridge_url is None and get_bartender_pid() is None:
        return None
    eff_timeout = min(timeout, runtime.time_remaining())
    url = f"{bridge_url or get_bridge_url()}/health"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=eff_timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception as e:
        log_debug(f"Health check failed for {url}: {e}")
    return None
