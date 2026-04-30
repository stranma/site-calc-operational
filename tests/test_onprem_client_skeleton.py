"""Tests for the OnPremClient skeleton (Phase C1).

Covers:
- C1.1: Exception hierarchy (subclass relations, envelope field propagation)
- C1.2: OnPremClient.health() (response parsing via respx mock)
"""

from __future__ import annotations

import respx
from httpx import Response

from site_calc_operational.api.onprem_client import HealthInfo, OnPremClient
from site_calc_operational.api.onprem_exceptions import (
    AuthenticationError,
    BusyError,
    CancelledError,
    IdempotencyConflict,
    NotImplementedOnServer,
    OnPremError,
    OnPremTimeoutError,
    ServerError,
    ValidationError,
    from_response,
)

# ---------------------------------------------------------------------------
# C1.1 -- Exception hierarchy
# ---------------------------------------------------------------------------


def test_hierarchy() -> None:
    """Failure mode: exception subclass is wrong -- library code raises base OnPremError
    instead of the specific subclass, or a subclass does not inherit from OnPremError."""
    assert issubclass(AuthenticationError, OnPremError)
    assert issubclass(ValidationError, OnPremError)
    assert issubclass(BusyError, OnPremError)
    assert issubclass(CancelledError, OnPremError)
    assert issubclass(NotImplementedOnServer, OnPremError)
    assert issubclass(ServerError, OnPremError)
    assert issubclass(OnPremTimeoutError, OnPremError)
    assert issubclass(IdempotencyConflict, OnPremError)
    # Tautology guard: each subclass must differ from the base
    assert AuthenticationError is not OnPremError
    assert ValidationError is not OnPremError
    assert BusyError is not OnPremError
    assert CancelledError is not OnPremError
    assert NotImplementedOnServer is not OnPremError
    assert ServerError is not OnPremError
    assert OnPremTimeoutError is not OnPremError
    assert IdempotencyConflict is not OnPremError


def test_carries_envelope() -> None:
    """Failure mode: from_response drops one or more envelope fields (code, message,
    details, tracking), so callers cannot inspect the structured error from the server."""
    body = {
        "error": {
            "code": "UNAUTHENTICATED",
            "message": "API key is revoked",
            "details": {"key_id": "abc123"},
            "tracking": "https://linear.app/issues/ALG-99",
        }
    }
    exc = from_response(401, body)
    assert isinstance(exc, AuthenticationError)
    assert exc.code == "UNAUTHENTICATED"
    assert exc.message == "API key is revoked"
    assert exc.details == {"key_id": "abc123"}
    assert exc.tracking == "https://linear.app/issues/ALG-99"
    assert exc.http_status == 401


def test_from_response_status_mapping() -> None:
    """Failure mode: from_response maps an HTTP status to the wrong exception class,
    so catch blocks in calling code silently pass over the wrong exception type."""
    assert isinstance(from_response(401, None), AuthenticationError)
    assert isinstance(from_response(422, None), ValidationError)
    assert isinstance(from_response(499, None), CancelledError)
    assert isinstance(from_response(501, None), NotImplementedOnServer)
    assert isinstance(from_response(503, None), BusyError)
    # Unmapped 5xx codes fall back to ServerError
    assert isinstance(from_response(500, None), ServerError)
    assert isinstance(from_response(502, None), ServerError)


# ---------------------------------------------------------------------------
# C1.2 -- OnPremClient.health()
# ---------------------------------------------------------------------------


@respx.mock
def test_health_parses() -> None:
    """Failure mode: health() fails to parse the server's JSON into HealthInfo, or
    returns a raw dict instead of a typed dataclass, breaking downstream type checks."""
    respx.get("http://stub/v1/health").mock(
        return_value=Response(
            200,
            json={
                "status": "ok",
                "site_calc_version": "1.5.2",
                "site_calc_commit_sha": "abc",
                "service_version": "0.1.0",
                "db_ok": True,
                "active_solve": False,
            },
        )
    )
    c = OnPremClient(base_url="http://stub", api_key="op_x")
    h = c.health()
    assert isinstance(h, HealthInfo)
    assert h.status == "ok"
    assert h.site_calc_version == "1.5.2"
    assert h.site_calc_commit_sha == "abc"
    assert h.service_version == "0.1.0"
    assert h.db_ok is True
    assert h.active_solve is False
