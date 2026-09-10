"""``OperationalClient``: the two planning calls and the stored runs, over plain HTTPS."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NoReturn
from uuid import UUID

import httpx

from site_calc_operational.exceptions import (
    BusyError,
    OperationalError,
    OperationalTimeoutError,
    TransportError,
    error_from_response,
)
from site_calc_operational.models import (
    DayAheadPlan,
    HealthInfo,
    PlanDayAheadRequest,
    PlanReservationRequest,
    ReservationPlan,
    RunDetail,
    RunEndpoint,
    RunsPage,
    RunStatus,
)

API_KEY_PREFIX = "op_"

DEFAULT_TIMEOUT_SECONDS = 1200.0
"""Read timeout for a planning call. The co-optimising planner can run for
around ten minutes on a two-service battery day; leave headroom."""


@dataclass(frozen=True)
class BusyRetry:
    """How to wait when the server answers ``503 BUSY`` (one plan at a time).

    The delay starts at ``initial_delay_seconds``, follows the server's
    ``Retry-After`` when present, doubles on each attempt and never exceeds
    ``max_delay_seconds``.
    """

    max_retries: int = 5
    initial_delay_seconds: float = 30.0
    max_delay_seconds: float = 120.0


DEFAULT_BUSY_RETRY = BusyRetry()


class OperationalClient:
    """Synchronous client for the operational planning server.

    Use it as a context manager so the connection pool is closed::

        with OperationalClient("https://operational.example.com", "op_...") as client:
            plan = client.plan_reservation(request)

    :param base_url: Server root, e.g. ``https://operational.example.com``.
    :param api_key: Bearer key issued by the server's operator; starts with ``op_``.
    :param timeout: Read timeout in seconds for every call (default 20 minutes).
    :param busy_retry: Policy for ``503 BUSY``; ``None`` raises
        :class:`~site_calc_operational.exceptions.BusyError` at once.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        busy_retry: BusyRetry | None = DEFAULT_BUSY_RETRY,
    ) -> None:
        if not api_key.startswith(API_KEY_PREFIX):
            raise ValueError(f"api_key must start with {API_KEY_PREFIX!r}")
        self._busy_retry = busy_retry
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=httpx.Timeout(timeout, connect=10.0),
        )
        self.last_response_headers: dict[str, str] = {}
        """Headers of the last response; ``X-Idempotent-Replay: true`` marks a replayed plan."""

    def __enter__(self) -> OperationalClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the connection pool."""
        self._http.close()

    @property
    def last_call_was_replay(self) -> bool:
        """True when the last planning call returned a stored result for a reused ``Idempotency-Key``."""
        return self.last_response_headers.get("x-idempotent-replay") == "true"

    # -- planning ---------------------------------------------------------

    def plan_reservation(
        self, request: PlanReservationRequest | dict[str, Any], *, idempotency_key: str | None = None
    ) -> ReservationPlan:
        """Plan the reservation bids of day D. Call before the reservation gate.

        :param request: A :class:`~site_calc_operational.models.PlanReservationRequest` or its dict form.
        :param idempotency_key: Any string unique to this request; sending it again
            within 24 hours returns the stored plan instead of planning twice. Use it
            whenever a call may be retried after a timeout.
        :raises ValidationError: The request cannot be planned as sent.
        :raises DayNotPlannableError: DST day, or a battery site without D+1 prices.
        :raises InfeasibleError: No plan satisfies the site and reservations.
        :raises BusyError: Another plan is running and retries are exhausted or disabled.
        """
        body = _as_dict(request, PlanReservationRequest)
        return ReservationPlan.model_validate(self._plan("/v1/plan/reservation", body, idempotency_key))

    def plan_day_ahead(
        self, request: PlanDayAheadRequest | dict[str, Any], *, idempotency_key: str | None = None
    ) -> DayAheadPlan:
        """Plan the day-ahead bids of day D with the cleared reservations honoured. Call before the day-ahead gate.

        :param request: A :class:`~site_calc_operational.models.PlanDayAheadRequest` or its dict form.
        :param idempotency_key: See :meth:`plan_reservation`.
        """
        body = _as_dict(request, PlanDayAheadRequest)
        return DayAheadPlan.model_validate(self._plan("/v1/plan/day-ahead", body, idempotency_key))

    # -- health and runs ---------------------------------------------------

    def health(self) -> HealthInfo:
        """Server liveness and versions. The server does not check the key on this route."""
        response = self._send("GET", "/v1/health")
        if response.status_code not in (200, 503):
            self._raise(response)
        return HealthInfo.model_validate(response.json())

    def get_run(self, run_id: UUID | str) -> RunDetail:
        """One of your stored runs with its request and response bodies."""
        response = self._send("GET", f"/v1/runs/{run_id}")
        if response.status_code != 200:
            self._raise(response)
        return RunDetail.model_validate(response.json())

    def list_runs(
        self,
        *,
        endpoint: RunEndpoint | None = None,
        status: RunStatus | None = None,
        limit: int | None = None,
        before: datetime | None = None,
    ) -> RunsPage:
        """Your runs, newest first. Pass the page's ``next_before`` as ``before`` to continue."""
        params: dict[str, Any] = {}
        if endpoint:
            params["endpoint"] = endpoint
        if status:
            params["status"] = status
        if limit is not None:
            params["limit"] = limit
        if before is not None:
            params["before"] = before.isoformat()
        response = self._send("GET", "/v1/runs", params=params)
        if response.status_code != 200:
            self._raise(response)
        return RunsPage.model_validate(response.json())

    def cancel_active(self) -> bool:
        """Interrupt the plan currently being computed. Returns False when the server was idle."""
        response = self._send("POST", "/v1/runs/active/cancel")
        if response.status_code == 204:
            return False
        if response.status_code != 200:
            self._raise(response)
        return bool(response.json().get("cancelled", True))

    # -- internals ---------------------------------------------------------

    def _plan(self, path: str, body: dict[str, Any], idempotency_key: str | None) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        policy = self._busy_retry
        attempt = 0
        delay = policy.initial_delay_seconds if policy else 0.0
        while True:
            response = self._send("POST", path, json=body, headers=headers)
            if response.status_code == 200:
                result: dict[str, Any] = response.json()
                return result
            if response.status_code == 503 and policy and attempt < policy.max_retries:
                err = error_from_response(503, _json_or_text(response), dict(response.headers))
                wait = err.retry_after_seconds if isinstance(err, BusyError) and err.retry_after_seconds else delay
                time.sleep(min(wait, policy.max_delay_seconds))
                delay = min(delay * 2, policy.max_delay_seconds)
                attempt += 1
                continue
            self._raise(response)

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise OperationalTimeoutError(code="TIMEOUT", message=f"{method} {path} timed out: {exc}") from exc
        except httpx.RequestError as exc:
            raise TransportError(code="TRANSPORT", message=f"{method} {path} failed: {exc}") from exc
        self.last_response_headers = {k.lower(): v for k, v in response.headers.items()}
        return response

    def _raise(self, response: httpx.Response) -> NoReturn:
        raise error_from_response(response.status_code, _json_or_text(response), dict(response.headers))


def _as_dict(request: Any, model: type[Any]) -> dict[str, Any]:
    if isinstance(request, dict):
        request = model.model_validate(request)
    wire: dict[str, Any] = request.model_dump(mode="json", exclude_none=True)
    return wire


def _json_or_text(response: httpx.Response) -> Any:
    try:
        body: Any = response.json()
    except ValueError:
        return response.text
    return body


__all__ = ["API_KEY_PREFIX", "DEFAULT_TIMEOUT_SECONDS", "BusyRetry", "OperationalClient", "OperationalError"]
