"""Sync HTTP client for the site-calc on-prem server.

Targets the synchronous on-prem server (server-onprem/) which differs from the
SaaS server targeted by :class:`~site_calc_operational.api.client.OperationalClient`.
The SaaS server uses async submit-then-poll semantics; the on-prem server blocks
until the solve completes and returns the full result in the response body.

Minimal public surface for Phase C1 (skeleton):
- :class:`HealthInfo` -- typed result of ``GET /v1/health``
- :class:`OnPremClient` -- constructor + :meth:`~OnPremClient.health` + context manager

Remaining methods (``device_planning``, ``get_run``, etc.) are added in C2-C4.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from site_calc_operational.api.onprem_exceptions import from_response


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

    :param base_url: Base URL of the on-prem server, e.g. ``"https://onprem.example.com"``.
    :param api_key: Bearer token with ``"op_"`` prefix.  Never logged.
    :param timeout_seconds: Per-request timeout in seconds.  The server enforces its own
        600-second cap on solver runs; set this to at least 600 for solve endpoints.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 600.0,
    ) -> None:
        """Initialise the client.

        :param base_url: Base URL of the on-prem server.
        :param api_key: Bearer token (``op_...``).
        :param timeout_seconds: Per-request timeout in seconds (default 600).
        """
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = httpx.Client(timeout=timeout_seconds)

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
