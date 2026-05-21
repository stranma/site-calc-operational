"""Sync HTTP client for the site-calc on-prem server.

Targets the synchronous on-prem server (server-onprem/) which differs from the
SaaS server targeted by :class:`~site_calc_operational.api.client.OperationalClient`.
The SaaS server uses async submit-then-poll semantics; the on-prem server blocks
until the solve completes and returns the full result in the response body.

Public surface for Phase C2:
- :class:`BackoffPolicy` -- configures 503 retry behaviour
- :class:`HealthInfo` -- typed result of ``GET /v1/health``
- :class:`OnPremClient` -- constructor + :meth:`~OnPremClient.health` +
  :meth:`~OnPremClient.device_planning` + context manager

Methods ``get_run``, ``list_runs``, and ``cancel_active`` were added in C3;
``optimal_bidding`` is added in C4.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, NoReturn

import httpx

from site_calc_operational.api.onprem_exceptions import from_response

# Sentinel for "use the default BackoffPolicy". Distinguishes "caller did not
# pass busy_retry at all" (use default) from "caller passed None" (disable
# retries). The previous implementation collapsed both to the default, which
# silently kept retries enabled even when callers explicitly opted out.
_USE_DEFAULT_RETRY: object = object()


@dataclass
class BackoffPolicy:
    """Policy for retrying requests that receive a 503 response.

    :param max_retries: Maximum number of retry attempts after the first 503.
        Set to 0 to attempt exactly once more before raising :class:`~.onprem_exceptions.BusyError`.
    :param initial_delay_seconds: Starting backoff delay in seconds.  Doubles each attempt.
    :param max_delay_seconds: Upper bound on the per-attempt delay in seconds.
    """

    max_retries: int = 3
    initial_delay_seconds: float = 10.0
    max_delay_seconds: float = 60.0


@dataclass
class HealthInfo:
    """Typed representation of the ``GET /v1/health`` response.

    :param status: ``"ok"`` when ``db_ok`` is true; ``"degraded"`` otherwise.
    :param site_calc_version: Semantic version of the bundled site-calc-core library.
    :param site_calc_commit_sha: Full git SHA of the bundled site-calc-core build.
    :param service_version: Semantic version of the on-prem server itself.
    :param db_ok: Whether the server could reach Postgres at check time.
    :param active_solve: Whether a solve is currently in progress.
    """

    status: str
    site_calc_version: str
    site_calc_commit_sha: str
    service_version: str
    db_ok: bool
    active_solve: bool


class OnPremClient:
    """Sync client for the site-calc on-prem server.

    Wraps a plain :class:`httpx.Client` (synchronous).  Use as a context manager
    to ensure the underlying connection pool is closed:

    .. code-block:: python

        with OnPremClient(base_url="https://onprem.example.com", api_key="op_...") as c:
            info = c.health()
            result = c.device_planning(request_payload)

    :param base_url: Base URL of the on-prem server, e.g. ``"https://onprem.example.com"``.
    :param api_key: Bearer token with ``"op_"`` prefix.  Never logged.
    :param timeout_seconds: Per-request timeout in seconds.  The server enforces its own
        600-second cap on solver runs; set this to at least 600 for solve endpoints.
    :param busy_retry: Retry policy for 503 responses.  Pass ``None`` to disable retries
        and surface :class:`~site_calc_operational.api.onprem_exceptions.BusyError` immediately.
        Defaults to :class:`BackoffPolicy` with ``max_retries=3``.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 600.0,
        busy_retry: BackoffPolicy | None = _USE_DEFAULT_RETRY,  # type: ignore[assignment]
    ) -> None:
        """Initialise the client.

        :param base_url: Base URL of the on-prem server.
        :param api_key: Bearer token (``op_...``).
        :param timeout_seconds: Per-request timeout in seconds (default 600).
        :param busy_retry: 503 retry policy. Pass ``None`` to disable retries
            (a 503 raises :class:`BusyError` immediately). Omit the argument
            to use a default :class:`BackoffPolicy` with ``max_retries=3``.
        """
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = httpx.Client(timeout=timeout_seconds)
        if busy_retry is _USE_DEFAULT_RETRY:
            self._busy_retry = BackoffPolicy()
        else:
            # busy_retry is now either a BackoffPolicy instance or None (disable retries).
            self._busy_retry = busy_retry  # type: ignore[assignment]
        # Mutable view of the most recent server response headers, used by the
        # MCP layer to detect ``X-Idempotent-Replay`` after a device_planning call.
        self.last_response_headers: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Resource management
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying HTTP connection pool.

        :raises httpx.HTTPStatusError: Propagated from httpx if a request was in flight.
        """
        self._client.close()

    def __enter__(self) -> "OnPremClient":
        """Enter the runtime context; returns self."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Exit the runtime context; closes the connection pool."""
        self.close()

    # ------------------------------------------------------------------
    # Unauthenticated endpoints
    # ------------------------------------------------------------------

    def health(self) -> HealthInfo:
        """Fetch server health from ``GET /v1/health``.

        This endpoint is unauthenticated.  It reports the bundled site-calc-core
        version, git SHA, service version, database reachability, and whether a
        solve is currently active.

        :returns: :class:`HealthInfo` populated from the server response.
        :raises OnPremError: If the server returns a non-2xx status.
        :raises httpx.TimeoutException: If the request times out.
        :raises httpx.RequestError: For network-level errors.
        """
        r = self._client.get(f"{self._base}/v1/health")
        if not r.is_success:
            _raise_onprem_error(r)
        body = r.json()
        return HealthInfo(
            status=body["status"],
            site_calc_version=body["site_calc_version"],
            site_calc_commit_sha=body["site_calc_commit_sha"],
            service_version=body["service_version"],
            db_ok=body["db_ok"],
            active_solve=body["active_solve"],
        )

    # ------------------------------------------------------------------
    # Authenticated solve endpoints
    # ------------------------------------------------------------------

    def device_planning(
        self,
        request: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Submit a device-planning solve via ``POST /v1/device-planning`` and return the result.

        The call blocks until the server completes the solve (synchronous HTTP).  On 503
        the method retries according to :attr:`busy_retry`; after all retries are exhausted
        it raises :class:`~site_calc_operational.api.onprem_exceptions.BusyError`.

        :param request: Request payload as a plain dict (matches ``DevicePlanningRequest`` schema).
        :param idempotency_key: If provided, the ``Idempotency-Key`` header is set on the wire
            request.  The server returns the cached response when the same key was used for a
            successful run within the last 24 hours.
        :returns: Response body as a plain dict (``DevicePlanningResponse`` shape).
        :raises BusyError: If the server returns 503 and all retry attempts are exhausted.
        :raises AuthenticationError: On 401.
        :raises ValidationError: On 422.
        :raises OnPremError: For any other non-200 response.
        :raises httpx.TimeoutException: If the client-side timeout fires.
        """
        return self._post_with_retry("/v1/device-planning", request, idempotency_key)

    # ------------------------------------------------------------------
    # Run read-back and cancel (C3)
    # ------------------------------------------------------------------

    def get_run(self, run_id: str) -> dict[str, Any]:
        """Fetch a single run record via ``GET /v1/runs/{id}``.

        Only the caller's own runs are returned.  A run owned by a different
        user, or a run that does not exist, both surface as a 404.

        :param run_id: UUID of the run to retrieve.
        :returns: Parsed run record dict (includes ``request`` and ``response`` fields).
        :raises AuthenticationError: On 401.
        :raises OnPremError: On 404 (not found or owned by another user) or any other
            non-200 response.
        :raises httpx.TimeoutException: If the request times out.
        """
        r = self._client.get(f"{self._base}/v1/runs/{run_id}", headers=self._headers)
        if r.status_code == 200:
            return r.json()  # type: ignore[no-any-return]
        _raise_onprem_error(r)

    def list_runs(
        self,
        *,
        endpoint: str | None = None,
        status: str | None = None,
        limit: int = 50,
        before: str | None = None,
    ) -> dict[str, Any]:
        """List the caller's recent runs via ``GET /v1/runs``.

        Runs are ordered by ``created_at DESC``.  Each item omits the full
        ``request`` / ``response`` bodies -- use :meth:`get_run` to fetch those.

        :param endpoint: Filter by endpoint name (``"device-planning"`` or
            ``"optimal-bidding"``).  Omit to return runs from any endpoint.
        :param status: Filter by run status (``"ok"``, ``"error"``, or
            ``"cancelled"``).  Omit to return runs with any status.
        :param limit: Maximum number of runs to return (server cap: 200).  Defaults to 50.
        :param before: ISO-8601 cursor timestamp.  Returns only runs created strictly
            before this timestamp, enabling pagination via the ``next_before`` field.
        :returns: Dict with keys ``"runs"`` (list) and ``"next_before"`` (ISO-8601 str or null).
        :raises AuthenticationError: On 401.
        :raises OnPremError: For any non-200 response.
        :raises httpx.TimeoutException: If the request times out.
        """
        params: dict[str, str | int] = {"limit": limit}
        if endpoint is not None:
            params["endpoint"] = endpoint
        if status is not None:
            params["status"] = status
        if before is not None:
            params["before"] = before

        r = self._client.get(f"{self._base}/v1/runs", params=params, headers=self._headers)
        if r.status_code == 200:
            return r.json()  # type: ignore[no-any-return]
        _raise_onprem_error(r)

    def cancel_active(self) -> dict[str, Any] | None:
        """Cancel the currently-running solve via ``POST /v1/runs/active/cancel``.

        Returns the cancelled run dict on 200 (a solve was in progress and was killed),
        or ``None`` on 204 (no solve was active).  Callers should distinguish these:
        ``None`` is not an error -- the server was simply idle.

        :returns: Cancelled run record dict on 200; ``None`` on 204 (nothing to cancel).
        :raises AuthenticationError: On 401.
        :raises OnPremError: For any other non-200/204 response.
        :raises httpx.TimeoutException: If the request times out.
        """
        r = self._client.post(f"{self._base}/v1/runs/active/cancel", headers=self._headers)
        if r.status_code == 200:
            return r.json()  # type: ignore[no-any-return]
        if r.status_code == 204:
            return None
        _raise_onprem_error(r)

    # ------------------------------------------------------------------
    # Bidding endpoints (C4)
    # ------------------------------------------------------------------

    def optimal_bidding(
        self,
        request: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Submit an optimal bidding request.

        Currently raises :class:`NotImplementedOnServer` because the on-prem server
        does not yet implement multi-step bid curve generation (see SPEC ss 3.3).
        The method shape mirrors :meth:`device_planning` so callers can switch to
        real bidding without code changes once the server endpoint flips from a
        501 stub to a real solve.

        :param request: payload (currently unused; the server returns 501 regardless).
        :param idempotency_key: passthrough; sent as Idempotency-Key header.
        :raises NotImplementedOnServer: always, for now.
        """
        return self._post_with_retry("/v1/optimal-bidding", request, idempotency_key)

    # ------------------------------------------------------------------
    # Reservation-bid endpoints (C5+)
    # ------------------------------------------------------------------

    def build_reservation_bids(
        self,
        request: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Build a day-ahead reservation-bid plan via ``POST /v1/reservation-bids``.

        The endpoint runs the planner and bundles the planner's own
        ``most_probable_realization`` and a re-evaluated ``expected_revenue``
        into the response, so callers get plan + realization + revenue
        cross-check in one round-trip. The call blocks until the server
        completes the solve.

        :param request: Payload matching the server's ``ReservationBidPlanRequest``
            (``sites``, ``timespan``, ``services``, ``acceptance``, optional
            ``expected_activation_revenue`` and ``assume_maximal``).
        :param idempotency_key: ``Idempotency-Key`` header passthrough. The
            server replays a successful run with this key within the TTL
            (24 hours by default; see ``Settings.idempotency_ttl_hours``).
        :returns: Dict with keys ``bids``, ``expected_revenue``, ``diagnostics``,
            ``most_probable_realization``, ``evaluation``.
        :raises BusyError: 503 after retries exhausted.
        :raises AuthenticationError: 401.
        :raises InfeasibleScenarioError: 422 ``INFEASIBLE`` (every Variant
            infeasible -- the site cannot dispatch).
        :raises ValidationError: 422 for schema / ``TRANSLATION_ERROR``.
        :raises OnPremError: For any other non-200 response.
        :raises httpx.TimeoutException: On client-side timeout.
        """
        return self._post_with_retry("/v1/reservation-bids", request, idempotency_key)

    def evaluate_reservation_bids(
        self,
        request: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Score a caller-supplied bid set via ``POST /v1/reservation-bids/evaluate``.

        Runs ``site_calc.planning.reservation_bids.expected_plan_revenue`` --
        the planner with the search removed. Useful for re-checking a planner
        result against an alternative acceptance distribution, or for scoring
        a hand-built bid set.

        :param request: Payload matching the server's
            ``ReservationBidEvaluateRequest`` (``sites``, ``timespan``, ``bids``,
            ``acceptance``, optional ``expected_activation_revenue``). At most
            one bid per interval; acceptance must cover every
            ``(service, interval)`` referenced by ``bids``.
        :param idempotency_key: ``Idempotency-Key`` header passthrough.
        :returns: ``{"expected_revenue": float}``.
        :raises InfeasibleScenarioError: 422 ``INFEASIBLE`` (site cannot honor
            the supplied bid set).
        :raises ValidationError: 422 ``TRANSLATION_ERROR`` (duplicate-interval
            bid, missing acceptance entry, invalid ``interval_start``, etc.).
        :raises BusyError, AuthenticationError, OnPremError: see
            :meth:`build_reservation_bids`.
        """
        return self._post_with_retry("/v1/reservation-bids/evaluate", request, idempotency_key)

    def most_probable_realization(
        self,
        request: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Single most-likely realization of a reservation-bid plan via
        ``POST /v1/reservation-bids/most-probable-realization``.

        For each bid, classifies it as cleared iff the acceptance distribution
        gives it at least 50% probability at its ``capacity_price``; the
        cleared subset pins the device, the rest is free. The day-ahead
        objective under that effective dispatch is the realized baseline.

        :param request: Payload matching the server's ``ReservationBidMPRRequest``
            (``sites``, ``timespan``, ``bids``, ``acceptance``).
        :param idempotency_key: ``Idempotency-Key`` header passthrough.
        :returns: Dict with keys ``contracts`` (the bids that clear),
            ``baseline_da``, ``realized_revenue``, ``joint_probability``.
        :raises InfeasibleScenarioError: 422 ``INFEASIBLE`` (the cleared subset
            cannot be honored by the site).
        :raises ValidationError, BusyError, AuthenticationError, OnPremError:
            see :meth:`build_reservation_bids`.
        """
        return self._post_with_retry("/v1/reservation-bids/most-probable-realization", request, idempotency_key)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _post_with_retry(
        self,
        path: str,
        request: dict[str, Any],
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        """POST *path* with JSON body, retrying on 503 per :attr:`busy_retry`.

        :param path: URL path relative to :attr:`_base` (e.g. ``"/v1/device-planning"``).
        :param request: JSON-serialisable request body.
        :param idempotency_key: Optional idempotency key; sets the ``Idempotency-Key`` header
            when not ``None``.
        :returns: Parsed JSON body from a 200 response.
        :raises BusyError: After all retries exhausted on repeated 503.
        :raises OnPremError: For any other non-200 status code.
        """
        headers = dict(self._headers)
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key

        attempt = 0
        delay = self._busy_retry.initial_delay_seconds if self._busy_retry else 0.0

        while True:
            r = self._client.post(f"{self._base}{path}", json=request, headers=headers)
            self.last_response_headers = dict(r.headers)
            if r.status_code == 200:
                return r.json()  # type: ignore[no-any-return]
            if r.status_code == 503 and self._busy_retry and attempt < self._busy_retry.max_retries:
                # Honor Retry-After if present and parseable; clamp to max_delay_seconds.
                try:
                    retry_after = float(r.headers.get("Retry-After", delay))
                except (ValueError, TypeError):
                    retry_after = delay
                sleep_secs = min(retry_after, self._busy_retry.max_delay_seconds)
                time.sleep(sleep_secs)
                attempt += 1
                delay = min(delay * 2, self._busy_retry.max_delay_seconds)
                continue
            _raise_onprem_error(r)


def _raise_onprem_error(response: httpx.Response) -> NoReturn:
    """Raise a typed OnPremError from an HTTP response, tolerating non-JSON bodies."""
    body: dict[str, Any] | None = None
    if response.content:
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            body = parsed
    raise from_response(response.status_code, body)
