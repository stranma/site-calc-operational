"""Errors raised by :class:`~site_calc_operational.OperationalClient`.

Every server-side failure arrives as ``{"error": {"code", "message", "details"}}``
and is raised as the :class:`OperationalError` subclass matching the code
(falling back to the HTTP status). Transport failures are wrapped too, so a
caller only ever handles this hierarchy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class OperationalError(Exception):
    """Base class: ``code`` is the server's machine-readable code, ``http_status`` the response status (0 if none)."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    http_status: int = field(default=0, compare=False)

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


class AuthenticationError(OperationalError):
    """401: the API key is missing, malformed, unknown, revoked, or its user is disabled."""


class NotFoundError(OperationalError):
    """404: no such run, or it belongs to another user."""


class IdempotencyConflictError(OperationalError):
    """409: the ``Idempotency-Key`` was already used with a different request body; ``details['run_id']`` names it."""


class RequestTooLargeError(OperationalError):
    """413: the request body exceeds the server's limit (10 MB by default)."""


class ValidationError(OperationalError):
    """422: the request is well-formed JSON but not a plannable request.

    ``VALIDATION_ERROR`` (``details['errors']`` lists the fields) or
    ``TRANSLATION_ERROR`` (an unknown timezone, a service the device does not
    declare, reservations over the power range, CHP pins on a battery site).
    """


class DayNotPlannableError(ValidationError):
    """422: the day itself cannot be planned.

    ``DST_DAY_UNSUPPORTED`` (the clocks change on D, or on D+1 for a battery
    site) or ``TWO_DAY_HORIZON_REQUIRED`` (a battery site without 96 D+1 prices).
    """


class InfeasibleError(OperationalError):
    """422 ``INFEASIBLE``: no plan satisfies the site, reservations and bounds.

    ``details['hint']`` says where to look.
    """


class UnboundedError(OperationalError):
    """422 ``UNBOUNDED``: a market interface has no finite limit."""


class CancelledError(OperationalError):
    """499: the run was interrupted by ``cancel_active``."""


class ServerError(OperationalError):
    """500 or any unexpected status: the server failed; retrying does not help."""


class BusyError(OperationalError):
    """503: another plan is being computed; ``retry_after_seconds`` says when to try again."""

    retry_after_seconds: float | None = None


class TransportError(OperationalError):
    """The server could not be reached or the connection broke."""


class OperationalTimeoutError(OperationalError):
    """The HTTP timeout elapsed before the plan arrived. Send an ``Idempotency-Key`` so a retry replays the result."""


_BY_CODE: dict[str, type[OperationalError]] = {
    "UNAUTHENTICATED": AuthenticationError,
    "NOT_FOUND": NotFoundError,
    "IDEMPOTENCY_KEY_REUSED": IdempotencyConflictError,
    "REQUEST_TOO_LARGE": RequestTooLargeError,
    "VALIDATION_ERROR": ValidationError,
    "TRANSLATION_ERROR": ValidationError,
    "DST_DAY_UNSUPPORTED": DayNotPlannableError,
    "TWO_DAY_HORIZON_REQUIRED": DayNotPlannableError,
    "INFEASIBLE": InfeasibleError,
    "UNBOUNDED": UnboundedError,
    "CANCELLED": CancelledError,
    "INTERNAL_ERROR": ServerError,
    "BUSY": BusyError,
}

_BY_STATUS: dict[int, type[OperationalError]] = {
    401: AuthenticationError,
    404: NotFoundError,
    409: IdempotencyConflictError,
    413: RequestTooLargeError,
    422: ValidationError,
    499: CancelledError,
    503: BusyError,
}


def error_from_response(http_status: int, body: Any, headers: dict[str, str] | None = None) -> OperationalError:
    """Build the matching exception from a non-2xx response.

    The server's ``error.code`` decides the class; the HTTP status is the
    fallback for a body without the envelope.

    :param http_status: Response status code.
    :param body: Parsed JSON body (any type) or the raw text.
    :param headers: Response headers, used for ``Retry-After`` on 503.
    """
    code, message, details = _parse_envelope(http_status, body)
    cls = _BY_CODE.get(code) or _BY_STATUS.get(http_status) or ServerError
    err = cls(code=code, message=message, details=details, http_status=http_status)
    if isinstance(err, BusyError):
        raw = (headers or {}).get("retry-after") or (headers or {}).get("Retry-After")
        try:
            err.retry_after_seconds = float(raw) if raw is not None else None
        except ValueError:
            err.retry_after_seconds = None
    return err


def _parse_envelope(http_status: int, body: Any) -> tuple[str, str, dict[str, Any]]:
    if isinstance(body, dict):
        env = body.get("error")
        if isinstance(env, dict):
            return (
                str(env.get("code") or f"HTTP_{http_status}"),
                str(env.get("message") or ""),
                dict(env.get("details") or {}),
            )
        # A bare FastAPI/proxy body: {"detail": ...}
        if "detail" in body:
            return f"HTTP_{http_status}", str(body["detail"]), {}
    text = body if isinstance(body, str) else ""
    return f"HTTP_{http_status}", text[:500], {}
