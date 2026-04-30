"""Tests for OnPremClient.device_planning() -- Phase C2.

Covers:
- C2.1: Happy path (200 returns body as dict)
- C2.1: 503 retry succeeds on second call
- C2.1: 503 exhausts all retries, raises BusyError
- C2.1: Idempotency-Key header passthrough on the wire
"""

from __future__ import annotations

import pytest
import respx
from httpx import Response

from site_calc_operational.api.onprem_client import BackoffPolicy, OnPremClient
from site_calc_operational.api.onprem_exceptions import BusyError

# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@respx.mock
def test_device_planning_returns_body() -> None:
    """Failure mode: device_planning() fails to return the parsed response body,
    or returns something other than the JSON dict sent by the server (e.g. the
    raw httpx.Response object, or None)."""
    respx.post("http://stub/v1/device-planning").mock(return_value=Response(200, json={"summary": {"profit": 42}}))
    c = OnPremClient(base_url="http://stub", api_key="op_x", busy_retry=None)
    out = c.device_planning({"sites": []})
    assert out == {"summary": {"profit": 42}}


# ---------------------------------------------------------------------------
# 503 retry: first call 503, second call 200
# ---------------------------------------------------------------------------


@respx.mock
def test_503_retries_and_succeeds() -> None:
    """Failure mode: the client either (a) raises BusyError after the first 503 without
    retrying, or (b) retries but does not verify the second call happened -- meaning the
    retry loop exits early or the route is misconfigured."""
    route = respx.post("http://stub/v1/device-planning")
    route.side_effect = [
        Response(503, headers={"Retry-After": "0"}, json={"error": {"code": "BUSY", "message": ""}}),
        Response(200, json={"summary": {}}),
    ]
    c = OnPremClient(
        base_url="http://stub",
        api_key="op_x",
        busy_retry=BackoffPolicy(max_retries=1, initial_delay_seconds=0, max_delay_seconds=0),
    )
    out = c.device_planning({})
    assert out == {"summary": {}}
    # Must verify the route was actually called twice -- not just that a body was returned.
    assert len(respx.calls) == 2


# ---------------------------------------------------------------------------
# 503 exhausts retries: BusyError raised
# ---------------------------------------------------------------------------


@respx.mock
def test_503_exhausts_raises_busyerror() -> None:
    """Failure mode: the client raises a generic OnPremError or Exception instead of the
    specific BusyError subclass, breaking caller code that catches BusyError by type."""
    respx.post("http://stub/v1/device-planning").mock(
        return_value=Response(
            503,
            headers={"Retry-After": "0"},
            json={"error": {"code": "BUSY", "message": "busy"}},
        )
    )
    c = OnPremClient(
        base_url="http://stub",
        api_key="op_x",
        busy_retry=BackoffPolicy(max_retries=1, initial_delay_seconds=0, max_delay_seconds=0),
    )
    with pytest.raises(BusyError) as exc_info:
        c.device_planning({})
    # Assert the specific type, not just that some exception was raised.
    assert type(exc_info.value) is BusyError
    assert exc_info.value.code == "BUSY"


# ---------------------------------------------------------------------------
# Idempotency-Key header passthrough
# ---------------------------------------------------------------------------


@respx.mock
def test_idempotency_key_passthrough() -> None:
    """Failure mode: the Idempotency-Key header is silently dropped before the request
    is sent, so the server never sees it and cannot perform idempotency replay."""
    respx.post("http://stub/v1/device-planning").mock(return_value=Response(200, json={"summary": {}}))
    c = OnPremClient(base_url="http://stub", api_key="op_x", busy_retry=None)
    c.device_planning({}, idempotency_key="my-unique-key-abc")
    # Verify the header was present in the actual wire request.
    assert respx.calls.last.request.headers["Idempotency-Key"] == "my-unique-key-abc"
