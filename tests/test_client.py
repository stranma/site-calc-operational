"""OperationalClient over a mocked server: wire, headers, errors, retries."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from site_calc_operational import (
    AuthenticationError,
    BusyError,
    BusyRetry,
    CancelledError,
    DayNotPlannableError,
    IdempotencyConflictError,
    InfeasibleError,
    NotFoundError,
    OperationalClient,
    OperationalTimeoutError,
    PlanDayAheadRequest,
    PlanReservationRequest,
    ServerError,
    TransportError,
    ValidationError,
)
from tests.conftest import API_KEY, BASE_URL


def _envelope(code: str, message: str = "m", **details: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


def test_api_key_prefix_is_checked() -> None:
    with pytest.raises(ValueError, match="op_"):
        OperationalClient(BASE_URL, "inv_wrongproduct")


def test_plan_reservation_sends_wire_and_parses(
    client: OperationalClient,
    mock_api: respx.MockRouter,
    reservation_request: PlanReservationRequest,
    reservation_plan_body: dict,
) -> None:
    route = mock_api.post("/v1/plan/reservation").mock(return_value=httpx.Response(200, json=reservation_plan_body))
    plan = client.plan_reservation(reservation_request, idempotency_key="k-1")
    sent = route.calls.last.request
    assert sent.headers["authorization"] == f"Bearer {API_KEY}"
    assert sent.headers["idempotency-key"] == "k-1"
    body = json.loads(sent.content)
    assert body["services"] == ["afrr_plus", "afrr_minus"] and len(body["ans_forecast"]) == 12
    assert body["site"]["devices"][0]["initial_soc_mwh"] == 1.0
    assert plan.expected_revenue.total == 54.0 and plan.run.horizon_qh == 192
    assert client.last_call_was_replay is False


def test_plan_reservation_accepts_dict_and_flags_replay(
    client: OperationalClient,
    mock_api: respx.MockRouter,
    reservation_request: PlanReservationRequest,
    reservation_plan_body: dict,
) -> None:
    mock_api.post("/v1/plan/reservation").mock(
        return_value=httpx.Response(200, json=reservation_plan_body, headers={"X-Idempotent-Replay": "true"})
    )
    plan = client.plan_reservation(reservation_request.model_dump(mode="json"), idempotency_key="k-1")
    assert plan.bids[0].volume_mw == 1.0
    assert client.last_call_was_replay is True


def test_plan_day_ahead(
    client: OperationalClient, mock_api: respx.MockRouter, site: Any, day: Any, day_ahead_plan_body: dict
) -> None:
    route = mock_api.post("/v1/plan/day-ahead").mock(return_value=httpx.Response(200, json=day_ahead_plan_body))
    req = PlanDayAheadRequest(
        site=site, day=day, cleared_reservations=[{"service": "afrr_plus", "block_index": 1, "volume_mw": 0.6}]
    )
    plan = client.plan_day_ahead(req)
    assert json.loads(route.calls.last.request.content)["cleared_reservations"][0]["volume_mw"] == 0.6
    assert plan.soc_end_mwh == 0.75 and plan.bids[95].volume_mw == 0.5 and plan.market_fees_eur == -3.0


@pytest.mark.parametrize(
    ("status", "code", "exc"),
    [
        (401, "UNAUTHENTICATED", AuthenticationError),
        (422, "VALIDATION_ERROR", ValidationError),
        (422, "TRANSLATION_ERROR", ValidationError),
        (422, "DST_DAY_UNSUPPORTED", DayNotPlannableError),
        (422, "TWO_DAY_HORIZON_REQUIRED", DayNotPlannableError),
        (422, "INFEASIBLE", InfeasibleError),
        (409, "IDEMPOTENCY_KEY_REUSED", IdempotencyConflictError),
        (499, "CANCELLED", CancelledError),
        (500, "INTERNAL_ERROR", ServerError),
        (503, "BUSY", BusyError),
    ],
)
def test_error_envelope_maps_to_exception(
    client: OperationalClient,
    mock_api: respx.MockRouter,
    reservation_request: PlanReservationRequest,
    status: int,
    code: str,
    exc: type[Exception],
) -> None:
    """Failure mode: two different 422s (bad input vs infeasible) must not collapse into one exception type."""
    mock_api.post("/v1/plan/reservation").mock(return_value=httpx.Response(status, json=_envelope(code, hint="h")))
    with pytest.raises(exc) as info:
        client.plan_reservation(reservation_request)
    assert info.value.code == code and info.value.http_status == status
    assert info.value.details == {"hint": "h"}
    assert str(info.value) == f"[{code}] m"
    assert isinstance(info.value, DayNotPlannableError) == (code in {"DST_DAY_UNSUPPORTED", "TWO_DAY_HORIZON_REQUIRED"})


def test_non_envelope_bodies_fall_back_to_status(
    client: OperationalClient, mock_api: respx.MockRouter, reservation_request: PlanReservationRequest
) -> None:
    mock_api.post("/v1/plan/reservation").mock(return_value=httpx.Response(502, text="<html>bad gateway</html>"))
    with pytest.raises(ServerError) as info:
        client.plan_reservation(reservation_request)
    assert info.value.code == "HTTP_502" and "bad gateway" in info.value.message
    mock_api.post("/v1/plan/reservation").mock(return_value=httpx.Response(404, json={"detail": "Not Found"}))
    with pytest.raises(NotFoundError, match="Not Found"):
        client.plan_reservation(reservation_request)


def test_busy_retries_honour_retry_after(
    mock_api: respx.MockRouter, reservation_request: PlanReservationRequest, reservation_plan_body: dict
) -> None:
    """Failure mode: the client hammering a single-slot server, or waiting forever on BUSY."""
    mock_api.post("/v1/plan/reservation").mock(
        side_effect=[
            httpx.Response(503, json=_envelope("BUSY"), headers={"Retry-After": "7"}),
            httpx.Response(503, json=_envelope("BUSY")),
            httpx.Response(200, json=reservation_plan_body),
        ]
    )
    with patch("site_calc_operational.client.time.sleep") as sleep:
        with OperationalClient(BASE_URL, API_KEY, busy_retry=BusyRetry(max_retries=3, initial_delay_seconds=5)) as c:
            plan = c.plan_reservation(reservation_request)
    assert plan.run.solve_seconds == 12.5
    assert [call.args[0] for call in sleep.call_args_list] == [7.0, 10.0]


def test_busy_gives_up_after_max_retries(
    mock_api: respx.MockRouter, reservation_request: PlanReservationRequest
) -> None:
    mock_api.post("/v1/plan/reservation").mock(
        return_value=httpx.Response(503, json=_envelope("BUSY"), headers={"Retry-After": "30"})
    )
    with patch("site_calc_operational.client.time.sleep") as sleep:
        with OperationalClient(BASE_URL, API_KEY, busy_retry=BusyRetry(max_retries=2, initial_delay_seconds=1)) as c:
            with pytest.raises(BusyError) as info:
                c.plan_reservation(reservation_request)
    assert sleep.call_count == 2 and info.value.retry_after_seconds == 30.0


def test_timeout_and_transport_errors_are_wrapped(
    client: OperationalClient, mock_api: respx.MockRouter, reservation_request: PlanReservationRequest
) -> None:
    mock_api.post("/v1/plan/reservation").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(OperationalTimeoutError, match="timed out"):
        client.plan_reservation(reservation_request)
    mock_api.post("/v1/plan/reservation").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(TransportError, match="refused"):
        client.plan_reservation(reservation_request)


def test_health_runs_and_cancel(client: OperationalClient, mock_api: respx.MockRouter) -> None:
    mock_api.get("/v1/health").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "service_version": "0.2.0",
                "site_calc_version": "1.5.0",
                "site_calc_commit_sha": "abc",
                "db_ok": True,
                "active_solve": False,
            },
        )
    )
    h = client.health()
    assert h.status == "ok" and h.service_version == "0.2.0" and not h.active_solve

    run_id = "6f1a1f34-3f4a-4a41-9f2e-2c8f7a1b9c10"
    summary = {
        "id": run_id,
        "endpoint": "plan-reservation",
        "status": "ok",
        "created_at": "2026-04-14T10:00:00+00:00",
        "finished_at": "2026-04-14T10:00:20+00:00",
        "duration_ms": 20000,
        "solver_status": "PlannerOk",
        "client_idempotency_key": "k-1",
    }
    runs = mock_api.get("/v1/runs").mock(
        return_value=httpx.Response(200, json={"runs": [summary], "next_before": "2026-04-14T10:00:00+00:00"})
    )
    page = client.list_runs(endpoint="plan-reservation", limit=10)
    assert runs.calls.last.request.url.params["endpoint"] == "plan-reservation"
    assert runs.calls.last.request.url.params["limit"] == "10"
    assert str(page.runs[0].id) == run_id and page.next_before is not None
    page2 = client.list_runs(before=page.next_before)
    assert runs.calls.last.request.url.params["before"] == "2026-04-14T10:00:00+00:00"
    assert page2.runs[0].endpoint == "plan-reservation"

    mock_api.get(f"/v1/runs/{run_id}").mock(
        return_value=httpx.Response(200, json={**summary, "request": {"a": 1}, "response": {"b": 2}})
    )
    detail = client.get_run(run_id)
    assert detail.request == {"a": 1} and detail.response == {"b": 2}
    mock_api.get("/v1/runs/00000000-0000-0000-0000-000000000000").mock(
        return_value=httpx.Response(404, json=_envelope("NOT_FOUND"))
    )
    with pytest.raises(NotFoundError):
        client.get_run("00000000-0000-0000-0000-000000000000")

    cancel = mock_api.post("/v1/runs/active/cancel")
    cancel.mock(return_value=httpx.Response(204))
    assert client.cancel_active() is False
    cancel.mock(return_value=httpx.Response(200, json={"cancelled": True}))
    assert client.cancel_active() is True
