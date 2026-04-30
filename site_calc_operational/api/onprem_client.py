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

Remaining methods (``get_run``, ``list_runs``, ``cancel_active``, etc.) are added in C3-C4.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from site_calc_operational.api.onprem_exceptions import from_response


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
        busy_retry: BackoffPolicy | None = None,
    ) -> None:
        """Initialise the client.

        :param base_url: Base URL of the on-prem server.
        :param api_key: Bearer token (``op_...``).
        :param timeout_seconds: Per-request timeout in seconds (default 600).
        :param busy_retry: 503 retry policy; ``None`` disables retries.
            Defaults to a fresh :class:`BackoffPolicy` with ``max_retries=3``.
        """
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = httpx.Client(timeout=timeout_seconds)
        self._busy_retry = busy_retry if busy_retry is not None else BackoffPolicy()

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
            raise from_response(r.status_code, r.json() if r.content else None)
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
            raise from_response(r.status_code, r.json() if r.content else None)
