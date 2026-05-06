"""Tests for OnPremClient run read-back and cancel methods -- Phase C3.

Covers:
- C3: get_run() happy path (200 returns parsed dict)
- C3: list_runs() sends query params on the wire (URL inspection, not just response body)
- C3: cancel_active() 200 case returns parsed dict
- C3: cancel_active() 204 case returns None (idle server, not an error)
"""

from __future__ import annotations

import respx
from httpx import Response

from site_calc_operational.api.onprem_client import OnPremClient
from site_calc_operational.api.onprem_exceptions import ClientError

# ---------------------------------------------------------------------------
# get_run
# ---------------------------------------------------------------------------


@respx.mock
def test_get_run() -> None:
    """Failure mode: get_run() fails to return the parsed run dict on 200,
    or returns the raw httpx.Response / raises instead of returning the body."""
    run_id = "550e8400-e29b-41d4-a716-446655440000"
    expected = {"id": run_id, "status": "ok", "endpoint": "device-planning"}
    respx.get(f"http://stub/v1/runs/{run_id}").mock(return_value=Response(200, json=expected))

    c = OnPremClient(base_url="http://stub", api_key="op_x", busy_retry=None)
    result = c.get_run(run_id)

    assert result == expected


@respx.mock
def test_get_run_non_json_error_raises_typed_client_error() -> None:
    """Failure mode: a proxy/plaintext 404 body raises JSONDecodeError instead of OnPremError."""
    run_id = "550e8400-e29b-41d4-a716-446655440000"
    respx.get(f"http://stub/v1/runs/{run_id}").mock(return_value=Response(404, text="not found"))

    c = OnPremClient(base_url="http://stub", api_key="op_x", busy_retry=None)

    try:
        c.get_run(run_id)
    except ClientError as exc:
        assert exc.http_status == 404
        assert exc.code == "UNKNOWN"
    else:  # pragma: no cover - pytest assertion clarity
        raise AssertionError("Expected ClientError")


# ---------------------------------------------------------------------------
# list_runs -- query params on the wire
# ---------------------------------------------------------------------------


@respx.mock
def test_list_runs_passes_query_params() -> None:
    """Failure mode: list_runs() builds the correct response body but silently drops
    one or more query parameters, so the server-side filter never takes effect.
    This test inspects the actual outgoing URL, not just the response body."""
    respx.get("http://stub/v1/runs").mock(return_value=Response(200, json={"runs": [], "next_before": None}))

    c = OnPremClient(base_url="http://stub", api_key="op_x", busy_retry=None)
    c.list_runs(endpoint="device-planning", status="ok", limit=10, before="2026-01-01T00:00:00Z")

    # Inspect the actual wire URL -- not the response body.
    query = respx.calls.last.request.url.query.decode()
    assert "endpoint=device-planning" in query
    assert "status=ok" in query
    assert "limit=10" in query
    assert "before=2026-01-01T00%3A00%3A00Z" in query or "before=2026-01-01T00:00:00Z" in query


# ---------------------------------------------------------------------------
# cancel_active -- 200 (a solve was running)
# ---------------------------------------------------------------------------


@respx.mock
def test_cancel_active_returns_status() -> None:
    """Failure mode: cancel_active() on a 200 response fails to return the parsed
    run dict (e.g. returns None or raises), so the caller cannot inspect the
    cancelled run's details."""
    cancelled_run = {"id": "abc", "status": "cancelled", "endpoint": "device-planning"}
    respx.post("http://stub/v1/runs/active/cancel").mock(return_value=Response(200, json=cancelled_run))

    c = OnPremClient(base_url="http://stub", api_key="op_x", busy_retry=None)
    result = c.cancel_active()

    assert result == cancelled_run


# ---------------------------------------------------------------------------
# cancel_active -- 204 (server was idle, nothing to cancel)
# ---------------------------------------------------------------------------


@respx.mock
def test_cancel_active_204_returns_none() -> None:
    """Failure mode: cancel_active() treats 204 as an error (raises) or returns an
    empty dict instead of None, preventing callers from distinguishing 'solve was
    cancelled' (200 + dict) from 'nothing was running' (204 + None)."""
    respx.post("http://stub/v1/runs/active/cancel").mock(return_value=Response(204))

    c = OnPremClient(base_url="http://stub", api_key="op_x", busy_retry=None)
    result = c.cancel_active()

    # Must be exactly None, not an empty dict or falsy substitute.
    assert result is None
