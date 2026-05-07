"""Exception hierarchy for OnPremClient.

Mirrors the server's error envelope defined in server-onprem/docs/SPEC.md section 3.8.

Every exception carries the structured fields from the server envelope:
- ``code``: machine-readable error code (e.g. ``"UNAUTHENTICATED"``)
- ``message``: human-readable explanation
- ``details``: optional dict with extra context (e.g. validation field errors)
- ``tracking``: optional tracking reference (Linear issue URL or null)
- ``http_status``: the HTTP status code that triggered this exception
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class OnPremError(Exception):
    """Base exception for all on-prem client errors.

    :param code: Machine-readable error code from the server envelope.
    :param message: Human-readable explanation.
    :param details: Optional dict with extra context.
    :param tracking: Optional tracking reference (e.g. Linear issue URL).
    :param http_status: HTTP status code that produced this error.
    """

    code: str
    message: str
    details: dict[str, Any] | None = None
    tracking: str | None = None
    http_status: int = field(default=0, compare=False)

    def __str__(self) -> str:
        """Return string representation of the error."""
        return f"[{self.code}] {self.message}"


class AuthenticationError(OnPremError):
    """Raised when the server returns 401 (missing, invalid, or revoked API key)."""

    ...


class ValidationError(OnPremError):
    """Raised when the server returns 422 (Pydantic validation failure).

    The ``details`` field typically contains per-field error information.
    """

    ...


class BusyError(OnPremError):
    """Raised when the server returns 503 after all retry attempts are exhausted.

    The on-prem server enforces single-slot concurrency; a second concurrent
    request receives 503 with a ``Retry-After`` header.
    """

    ...


class CancelledError(OnPremError):
    """Raised when the server returns 499 (run cancelled mid-solve via cancel-active)."""

    ...


class NotImplementedOnServer(OnPremError):  # noqa: N818
    """Raised when the server returns 501 (endpoint not yet implemented on the server).

    Expected for ``POST /v1/optimal-bidding`` in the MVP.
    """

    ...


class ServerError(OnPremError):
    """Raised for unexpected 5xx responses not covered by a more specific exception."""

    ...


class ClientError(OnPremError):
    """Raised for unexpected 4xx responses not covered by a more specific exception."""

    ...


class OnPremTimeoutError(OnPremError):
    """Raised when a client-side timeout occurs (httpx.TimeoutException)."""

    ...


class IdempotencyConflict(OnPremError):  # noqa: N818
    """Raised when the server returns a specific idempotency conflict code (future use)."""

    ...


class InfeasibleScenarioError(OnPremError):
    """Raised when the optimizer reports the problem has no feasible solution.

    Sent by the on-prem server with HTTP 422 and ``code="INFEASIBLE"`` when the
    LP/MIP solver determines no assignment of variables satisfies all
    constraints. Common causes: demand exceeds supply, profile lengths
    inconsistent with timespan, contradictory schedule constraints
    (``must_run`` overlapping ``can_run=0``), missing market interface for a
    required material.
    """

    ...


class UnboundedScenarioError(OnPremError):
    """Raised when the optimizer reports the objective is unbounded.

    Sent with HTTP 422 and ``code="UNBOUNDED"``. Usually means the request
    lacks a price-bounded constraint on a free source of revenue (e.g. an
    export device with no maximum flow).
    """

    ...


# Map HTTP status codes to exception classes.
# Codes not listed here fall back to ClientError for 4xx and ServerError otherwise.
_BY_HTTP: dict[int, type[OnPremError]] = {
    401: AuthenticationError,
    422: ValidationError,
    499: CancelledError,
    501: NotImplementedOnServer,
    503: BusyError,
}

# Map error-envelope codes to exception classes. Wins over the HTTP-status map
# so that two distinct semantics on the same status (e.g. schema validation vs
# infeasible LP, both 422) get distinct exception types.
_BY_CODE: dict[str, type[OnPremError]] = {
    "INFEASIBLE": InfeasibleScenarioError,
    "UNBOUNDED": UnboundedScenarioError,
}


def from_response(http_status: int, body: dict[str, Any] | None) -> OnPremError:
    """Build a typed :class:`OnPremError` from an HTTP status code and parsed body.

    Extracts ``code``, ``message``, ``details``, and ``tracking`` from the
    server's structured error envelope (``body["error"]``).  Falls back to
    sensible defaults when fields are absent.

    Resolution order for the exception class:

    1. Match on ``error.code`` (e.g. ``"INFEASIBLE"`` -> :class:`InfeasibleScenarioError`).
    2. Match on HTTP status (e.g. 401 -> :class:`AuthenticationError`).
    3. Fall back to :class:`ClientError` for unmapped 4xx, :class:`ServerError` otherwise.

    :param http_status: The HTTP status code returned by the server.
    :param body: The parsed JSON response body, or ``None`` if the body was empty.
    :returns: A typed ``OnPremError`` subclass instance.
    """
    err = (body or {}).get("error") or {}
    raw_code = err.get("code")  # None when the envelope is missing entirely
    cls = (_BY_CODE.get(raw_code) if raw_code else None) or _BY_HTTP.get(http_status)
    if cls is None:
        cls = ClientError if 400 <= http_status < 500 else ServerError
    return cls(
        code=raw_code or "UNKNOWN",
        message=err.get("message", ""),
        details=err.get("details"),
        tracking=err.get("tracking"),
        http_status=http_status,
    )
