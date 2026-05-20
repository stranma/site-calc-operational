"""Tests for OnPremClient's reservation-bid methods.

Mirrors ``test_onprem_device_planning.py``'s structure for each of the three
endpoints exposed by ``server-onprem`` v0.2:

* ``build_reservation_bids`` -> ``POST /v1/reservation-bids``
* ``evaluate_reservation_bids`` -> ``POST /v1/reservation-bids/evaluate``
* ``most_probable_realization`` -> ``POST /v1/reservation-bids/most-probable-realization``

Covers per-method: happy path, 503 retry, 503 exhaustion -> BusyError,
Idempotency-Key passthrough; plus shared cases for the error-mapping (422
INFEASIBLE -> InfeasibleScenarioError, 422 TRANSLATION_ERROR -> ValidationError,
401 -> AuthenticationError) on the planner endpoint as a representative.
"""

from __future__ import annotations

import base64

import pytest
import respx
from httpx import Response

from site_calc_operational.api.onprem_client import BackoffPolicy, OnPremClient
from site_calc_operational.api.onprem_exceptions import (
    AuthenticationError,
    BusyError,
    InfeasibleScenarioError,
    ValidationError,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_BASE = "http://stub"


def _client(busy_retry: object = None) -> OnPremClient:
    return OnPremClient(base_url=_BASE, api_key="op_x", busy_retry=busy_retry)


# Smallest valid-looking payload accepted by the client (server-side validation
# is mocked, so we don't need a real fixture here -- the goal is to exercise
# the client wiring, not the server contract).
_MIN_REQUEST: dict = {"sites": []}

# ---------------------------------------------------------------------------
# build_reservation_bids -- POST /v1/reservation-bids
# ---------------------------------------------------------------------------


@respx.mock
def test_build_reservation_bids_returns_body() -> None:
    """Failure mode: build_reservation_bids() fails to return the parsed
    response body, or returns something other than the JSON dict sent by
    the server."""
    expected = {
        "bids": [
            {
                "service": "afrr_plus",
                "interval_start": "2026-05-13T00:00:00+02:00",
                "volume_mw": 1.0,
                "capacity_price": 25.4,
            }
        ],
        "expected_revenue": 407.46,
        "diagnostics": {"winner_is_maximal": True},
        "most_probable_realization": {
            "contracts": [],
            "baseline_da": 0.0,
            "realized_revenue": 0.0,
            "joint_probability": 1.0,
        },
        "evaluation": {"expected_revenue": 407.46},
    }
    respx.post(f"{_BASE}/v1/reservation-bids").mock(return_value=Response(200, json=expected))
    out = _client().build_reservation_bids(_MIN_REQUEST)
    assert out == expected


@respx.mock
def test_build_reservation_bids_503_retries_and_succeeds() -> None:
    """The 503 retry policy must apply to the new endpoint too."""
    route = respx.post(f"{_BASE}/v1/reservation-bids")
    route.side_effect = [
        Response(503, headers={"Retry-After": "0"}, json={"error": {"code": "BUSY", "message": ""}}),
        Response(200, json={"bids": [], "expected_revenue": 0.0}),
    ]
    c = _client(busy_retry=BackoffPolicy(max_retries=1, initial_delay_seconds=0, max_delay_seconds=0))
    out = c.build_reservation_bids(_MIN_REQUEST)
    assert out == {"bids": [], "expected_revenue": 0.0}
    assert len(respx.calls) == 2


@respx.mock
def test_build_reservation_bids_503_exhausts_raises_busyerror() -> None:
    respx.post(f"{_BASE}/v1/reservation-bids").mock(
        return_value=Response(
            503,
            headers={"Retry-After": "0"},
            json={"error": {"code": "BUSY", "message": "busy"}},
        )
    )
    c = _client(busy_retry=BackoffPolicy(max_retries=0, initial_delay_seconds=0, max_delay_seconds=0))
    with pytest.raises(BusyError) as exc_info:
        c.build_reservation_bids(_MIN_REQUEST)
    assert type(exc_info.value) is BusyError


@respx.mock
def test_build_reservation_bids_idempotency_key_passthrough() -> None:
    respx.post(f"{_BASE}/v1/reservation-bids").mock(return_value=Response(200, json={}))
    _client().build_reservation_bids(_MIN_REQUEST, idempotency_key="rb-key-1")
    assert respx.calls.last.request.headers["Idempotency-Key"] == "rb-key-1"


# ---------------------------------------------------------------------------
# evaluate_reservation_bids -- POST /v1/reservation-bids/evaluate
# ---------------------------------------------------------------------------


@respx.mock
def test_evaluate_reservation_bids_returns_body() -> None:
    expected = {"expected_revenue": 407.461314}
    respx.post(f"{_BASE}/v1/reservation-bids/evaluate").mock(return_value=Response(200, json=expected))
    out = _client().evaluate_reservation_bids(_MIN_REQUEST)
    assert out == expected


@respx.mock
def test_evaluate_reservation_bids_503_retries_and_succeeds() -> None:
    route = respx.post(f"{_BASE}/v1/reservation-bids/evaluate")
    route.side_effect = [
        Response(503, headers={"Retry-After": "0"}, json={"error": {"code": "BUSY", "message": ""}}),
        Response(200, json={"expected_revenue": 1.0}),
    ]
    c = _client(busy_retry=BackoffPolicy(max_retries=1, initial_delay_seconds=0, max_delay_seconds=0))
    out = c.evaluate_reservation_bids(_MIN_REQUEST)
    assert out == {"expected_revenue": 1.0}
    assert len(respx.calls) == 2


@respx.mock
def test_evaluate_reservation_bids_idempotency_key_passthrough() -> None:
    respx.post(f"{_BASE}/v1/reservation-bids/evaluate").mock(return_value=Response(200, json={}))
    _client().evaluate_reservation_bids(_MIN_REQUEST, idempotency_key="eval-key-2")
    assert respx.calls.last.request.headers["Idempotency-Key"] == "eval-key-2"


# ---------------------------------------------------------------------------
# most_probable_realization -- POST /v1/reservation-bids/most-probable-realization
# ---------------------------------------------------------------------------


@respx.mock
def test_most_probable_realization_returns_body() -> None:
    expected = {
        "contracts": [
            {
                "service": "afrr_plus",
                "interval_start": "2026-05-13T00:00:00+02:00",
                "volume_mw": 1.0,
                "capacity_price": 25.4,
            }
        ],
        "baseline_da": 12.34,
        "realized_revenue": 419.91,
        "joint_probability": 0.083,
    }
    respx.post(f"{_BASE}/v1/reservation-bids/most-probable-realization").mock(return_value=Response(200, json=expected))
    out = _client().most_probable_realization(_MIN_REQUEST)
    assert out == expected


@respx.mock
def test_most_probable_realization_503_retries_and_succeeds() -> None:
    route = respx.post(f"{_BASE}/v1/reservation-bids/most-probable-realization")
    route.side_effect = [
        Response(503, headers={"Retry-After": "0"}, json={"error": {"code": "BUSY", "message": ""}}),
        Response(200, json={"contracts": [], "baseline_da": 0.0, "realized_revenue": 0.0, "joint_probability": 1.0}),
    ]
    c = _client(busy_retry=BackoffPolicy(max_retries=1, initial_delay_seconds=0, max_delay_seconds=0))
    out = c.most_probable_realization(_MIN_REQUEST)
    assert out["joint_probability"] == 1.0
    assert len(respx.calls) == 2


@respx.mock
def test_most_probable_realization_idempotency_key_passthrough() -> None:
    respx.post(f"{_BASE}/v1/reservation-bids/most-probable-realization").mock(return_value=Response(200, json={}))
    _client().most_probable_realization(_MIN_REQUEST, idempotency_key="mpr-key-3")
    assert respx.calls.last.request.headers["Idempotency-Key"] == "mpr-key-3"


# ---------------------------------------------------------------------------
# Error mapping (use build_reservation_bids as the representative endpoint;
# the dispatch lives in from_response so the same mapping applies to all three)
# ---------------------------------------------------------------------------


@respx.mock
def test_422_translation_error_raises_validation_error() -> None:
    """Server's 422 with code TRANSLATION_ERROR (e.g. zero ANS-capable devices)
    must surface as ValidationError -- the HTTP-status default."""
    respx.post(f"{_BASE}/v1/reservation-bids").mock(
        return_value=Response(
            422,
            json={"error": {"code": "TRANSLATION_ERROR", "message": "exactly one ANS-capable device required"}},
        )
    )
    with pytest.raises(ValidationError) as exc_info:
        _client().build_reservation_bids(_MIN_REQUEST)
    assert exc_info.value.code == "TRANSLATION_ERROR"
    assert exc_info.value.http_status == 422


@respx.mock
def test_422_infeasible_raises_infeasible_scenario_error_with_debug_lp() -> None:
    """Server's 422 with code INFEASIBLE must raise InfeasibleScenarioError
    (the by-code dispatch wins over the by-status dispatch) and must carry
    the server's debug_lp_b64 payload through ``details`` for caller use."""
    lp_bytes = b"\\ a tiny LP file\nMinimize\n obj: x\nSubject To\n c1: x >= 0\nEnd\n"
    body = {
        "error": {
            "code": "INFEASIBLE",
            "message": "every Variant infeasible",
            "details": {
                "hint": "check budgets",
                "debug_lp_filename": "debug_problem.lp",
                "debug_lp_size_bytes": len(lp_bytes),
                "debug_lp_b64": base64.b64encode(lp_bytes).decode("ascii"),
            },
        }
    }
    respx.post(f"{_BASE}/v1/reservation-bids").mock(return_value=Response(422, json=body))
    with pytest.raises(InfeasibleScenarioError) as exc_info:
        _client().build_reservation_bids(_MIN_REQUEST)
    assert exc_info.value.code == "INFEASIBLE"
    # The base64-decoded LP is available to power users via details.
    assert exc_info.value.details is not None
    decoded = base64.b64decode(exc_info.value.details["debug_lp_b64"])
    assert decoded == lp_bytes


@respx.mock
def test_401_raises_authentication_error() -> None:
    respx.post(f"{_BASE}/v1/reservation-bids").mock(
        return_value=Response(401, json={"error": {"code": "UNAUTHENTICATED", "message": "no key"}})
    )
    with pytest.raises(AuthenticationError):
        _client().build_reservation_bids(_MIN_REQUEST)


@respx.mock
def test_request_body_is_passed_through() -> None:
    """The client must send the caller-supplied dict verbatim as the JSON body."""
    respx.post(f"{_BASE}/v1/reservation-bids").mock(return_value=Response(200, json={}))
    payload = {"sites": [{"site_id": "s1", "devices": []}], "services": ["afrr_plus"]}
    _client().build_reservation_bids(payload)
    import json

    sent = json.loads(respx.calls.last.request.content)
    assert sent == payload
