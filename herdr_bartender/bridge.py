"""HTTP client for the Bartender NotchBar bridge.

Every request (Plan §8):

* targets the literal loopback ``http://127.0.0.1:<port>`` (no hostname
  resolution, no other hosts or schemes);
* goes through an opener with no proxy handler and no redirect following, so a
  3xx is surfaced and classified ``unexpected_redirect``;
* is gated on a running Bartender process (``process.get_bartender_pid()``),
  whether or not an explicit ``bridge_url`` is passed.

``classify_response()`` maps a response to a ``DeliveryResult`` with the Plan
§3.3 ``delivery_error`` codes. ``deliver_event()`` is the structured API;
``post_bartender_event()`` / ``_raw_post_event()`` keep the legacy
``(success, non_retryable)`` tuple for existing callers.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional, Tuple

from . import process, runtime
from .config import DEFAULT_HOST, get_bridge_url
from .log import log_debug

OUTCOME_SUCCESS = "success"
OUTCOME_NON_RETRYABLE = "non_retryable"
OUTCOME_RETRYABLE = "retryable"

ERR_BRIDGE_REJECTED = "bridge_rejected"
ERR_UNEXPECTED_REDIRECT = "unexpected_redirect"
ERR_CLIENT = "4xx_client_error"
ERR_SERVER = "5xx_server_error"
ERR_NETWORK = "network_timeout"
ERR_INVALID_RESPONSE = "invalid_response"
ERR_BARTENDER_NOT_RUNNING = "bartender_not_running"
ERR_INVALID_BRIDGE_URL = "invalid_bridge_url"

DEFAULT_EVENT_TIMEOUT = 0.2
DEFAULT_HEALTH_TIMEOUT = 0.6
MIN_SOCKET_TIMEOUT = 0.05
NETWORK_BUDGET_RESERVE = 0.3
MAX_RESPONSE_BYTES = 65536
UNIT_TESTING_ENV = "HERDR_BARTENDER_UNIT_TESTING"


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of one bridge request: ``outcome`` is success|non_retryable|retryable."""

    outcome: str
    error: Optional[str] = None
    http_status: Optional[int] = None

    @property
    def success(self) -> bool:
        return self.outcome == OUTCOME_SUCCESS

    @property
    def non_retryable(self) -> bool:
        return self.outcome == OUTCOME_NON_RETRYABLE

    @property
    def retryable(self) -> bool:
        return self.outcome == OUTCOME_RETRYABLE

    def as_tuple(self) -> Tuple[bool, bool]:
        """Legacy ``(success, is_non_retryable)`` shape."""
        return (self.success, self.non_retryable)

    def as_dict(self) -> dict:
        return {"outcome": self.outcome, "error": self.error, "http_status": self.http_status}


# -- classification (pure) -------------------------------------------------------
def _classify_ok_body(body: Optional[bytes]) -> DeliveryResult:
    try:
        parsed = json.loads((body or b"").decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return DeliveryResult(OUTCOME_RETRYABLE, ERR_INVALID_RESPONSE, 200)
    if not isinstance(parsed, dict):
        return DeliveryResult(OUTCOME_RETRYABLE, ERR_INVALID_RESPONSE, 200)
    if parsed.get("ok") is True:
        return DeliveryResult(OUTCOME_SUCCESS, None, 200)
    return DeliveryResult(OUTCOME_NON_RETRYABLE, ERR_BRIDGE_REJECTED, 200)


def classify_response(status: Optional[int], body: Optional[bytes] = None,
                      error: Optional[BaseException] = None) -> DeliveryResult:
    """Plan §3.3: map (HTTP status, body, transport error) to a DeliveryResult.

    ``status is None`` means no HTTP response was received (``error`` holds the
    transport failure, if any): a retryable ``network_timeout``.
    """
    if status is None:
        return DeliveryResult(OUTCOME_RETRYABLE, ERR_NETWORK, None)
    if status == 200:
        return _classify_ok_body(body)
    if 300 <= status < 400:
        return DeliveryResult(OUTCOME_NON_RETRYABLE, ERR_UNEXPECTED_REDIRECT, status)
    if 400 <= status < 500:
        return DeliveryResult(OUTCOME_NON_RETRYABLE, ERR_CLIENT, status)
    if 500 <= status < 600:
        return DeliveryResult(OUTCOME_RETRYABLE, ERR_SERVER, status)
    return DeliveryResult(OUTCOME_RETRYABLE, ERR_INVALID_RESPONSE, status)


# -- transport -------------------------------------------------------------------
class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib then raises HTTPError with the 3xx code."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - stdlib hook
        return None


def build_bridge_opener() -> urllib.request.OpenerDirector:
    """Opener with no redirect following and an empty ProxyHandler.

    ``ProxyHandler({})`` replaces the default handler that reads HTTP(S)_PROXY /
    ALL_PROXY, so bridge traffic stays on loopback whatever the environment says.
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())


_OPENER = build_bridge_opener()


def default_bridge_url() -> str:
    return get_bridge_url()


def validated_bridge_url(bridge_url: Optional[str] = None) -> Optional[str]:
    """Normalise ``bridge_url`` (default: the configured bridge) to ``http://127.0.0.1:<port>``.

    Returns None for anything else (other hosts, names, schemes, credentials, paths).
    """
    try:
        parts = urllib.parse.urlsplit(bridge_url or default_bridge_url())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "http" or parts.hostname != DEFAULT_HOST or port is None:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    return f"http://{DEFAULT_HOST}:{port}"


def socket_timeout(timeout: float) -> float:
    """Plan §6.1: ``min(timeout, max(0.05, time_remaining() - 0.3))`` on the bounded path.

    Unbounded modes (reconciler, --cleanup, --replay-orphans) use the caller's timeout.
    """
    if not runtime.deadline_bounded():
        return timeout
    return min(timeout, max(MIN_SOCKET_TIMEOUT, runtime.time_remaining() - NETWORK_BUDGET_RESERVE))


def _bartender_gate_open(bridge_url: Optional[str]) -> bool:
    """Plan §1 L135 / §8 L1085: only talk to the bridge while a Bartender process runs."""
    if process.get_bartender_pid() is not None:
        return True
    log_debug("Refusing to contact loopback listener: Bartender process not running (or unknown)")
    return False


def _guard_critical_section() -> None:
    if runtime.IN_CRITICAL_SECTION:
        log_debug("FATAL: Network I/O attempted while holding critical section file lock!")
        if os.environ.get(UNIT_TESTING_ENV):
            raise AssertionError("Network I/O attempted while holding critical section file lock!")


def _perform(req: urllib.request.Request, timeout: float
             ) -> Tuple[Optional[int], Optional[bytes], Optional[BaseException]]:
    """Run one request: (status, body, None) on any HTTP response, (None, None, error) otherwise."""
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(MAX_RESPONSE_BYTES), None
    except urllib.error.HTTPError as e:
        try:
            body = e.read(MAX_RESPONSE_BYTES)
        except (OSError, http.client.HTTPException):
            body = b""
        finally:
            e.close()
        return e.code, body, None
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
        return None, None, e


def _prepare(path: str, bridge_url: Optional[str]) -> Tuple[Optional[str], Optional[DeliveryResult]]:
    """Validate the target and pass the liveness gate: (url, None) or (None, refusal)."""
    base = validated_bridge_url(bridge_url)
    if base is None:
        log_debug(f"Refusing non-loopback bridge URL: {bridge_url!r}")
        return None, DeliveryResult(OUTCOME_NON_RETRYABLE, ERR_INVALID_BRIDGE_URL)
    if not _bartender_gate_open(bridge_url):
        return None, DeliveryResult(OUTCOME_RETRYABLE, ERR_BARTENDER_NOT_RUNNING)
    return f"{base}{path}", None


# -- public API ------------------------------------------------------------------
def send_event(payload: dict, timeout: float = DEFAULT_EVENT_TIMEOUT,
               bridge_url: Optional[str] = None) -> DeliveryResult:
    """POST one event (no retries) and classify the response."""
    _guard_critical_section()
    url, refusal = _prepare("/event", bridge_url)
    if refusal is not None:
        return refusal
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    status, body, error = _perform(req, socket_timeout(timeout))
    result = classify_response(status, body, error)
    if not result.success:
        log_debug(f"Bridge POST {url} -> {result.error} (status={status}, error={error!r})")
    return result


MINIMAL_ENDED_AGENT = "Herdr"


def minimal_ended_payload(payload: dict) -> dict:
    """Plan §3.3 L230/L232, R19: ``{"state": "Ended", "agent": "Herdr", "session_id": sid}``."""
    return {"state": "Ended", "agent": MINIMAL_ENDED_AGENT, "session_id": payload.get("session_id")}


def deliver_event(payload: dict, timeout: float = DEFAULT_EVENT_TIMEOUT,
                  bridge_url: Optional[str] = None) -> DeliveryResult:
    """POST an event; a failed ``Ended`` is retried once with the minimal payload (Plan §3.3).

    The retry is skipped when the request never reached the bridge for a reason a
    retry cannot fix (gate refused, invalid URL) or the budget cannot fit it.
    """
    result = send_event(payload, timeout=timeout, bridge_url=bridge_url)
    if result.success or payload.get("state") != "Ended":
        return result
    if result.error in (ERR_BARTENDER_NOT_RUNNING, ERR_INVALID_BRIDGE_URL):
        return result
    min_payload = minimal_ended_payload(payload)
    if min_payload == payload or not runtime.budget_allows(NETWORK_BUDGET_RESERVE):
        return result
    log_debug("Retrying Ended event with minimal payload")
    return send_event(min_payload, timeout=timeout, bridge_url=bridge_url)


def _raw_post_event(payload: dict, timeout: float = DEFAULT_EVENT_TIMEOUT,
                    bridge_url: Optional[str] = None) -> Tuple[bool, bool]:
    """Legacy tuple API for ``send_event()``: (success, is_non_retryable)."""
    return send_event(payload, timeout=timeout, bridge_url=bridge_url).as_tuple()


def post_bartender_event(payload: dict, timeout: float = DEFAULT_EVENT_TIMEOUT,
                         bridge_url: Optional[str] = None) -> Tuple[bool, bool]:
    """Legacy tuple API for ``deliver_event()``: (success, is_non_retryable)."""
    return deliver_event(payload, timeout=timeout, bridge_url=bridge_url).as_tuple()


def check_bridge_health(timeout: float = DEFAULT_HEALTH_TIMEOUT, bridge_url: Optional[str] = None) -> Optional[dict]:
    """GET /health; the parsed JSON object on HTTP 200, else None (gated like every request)."""
    url, refusal = _prepare("/health", bridge_url)
    if refusal is not None:
        return None
    status, body, error = _perform(urllib.request.Request(url, method="GET"), socket_timeout(timeout))
    if status != 200:
        log_debug(f"Health check failed for {url}: status={status} error={error!r}")
        return None
    try:
        parsed = json.loads((body or b"").decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        log_debug(f"Health check for {url} returned invalid JSON: {e}")
        return None
    return parsed if isinstance(parsed, dict) else None
