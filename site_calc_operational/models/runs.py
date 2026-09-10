"""Health and the stored runs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from site_calc_operational.models._base import ResponseModel

RunStatus = Literal["ok", "error", "cancelled"]
RunEndpoint = Literal["plan-reservation", "plan-day-ahead"]


class HealthInfo(ResponseModel):
    """What ``/v1/health`` reports."""

    status: str = Field(description="``ok`` or ``degraded``.")
    service_version: str = Field(description="Server version; its MAJOR.MINOR should match this package's.")
    site_calc_version: str = Field(description="Version of the optimisation engine inside the server.")
    site_calc_commit_sha: str
    db_ok: bool
    active_solve: bool = Field(description="True while a plan is being computed; a new call would get BUSY.")


class RunSummary(ResponseModel):
    """One stored run without its bodies."""

    id: UUID
    endpoint: RunEndpoint
    status: RunStatus
    created_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    solver_status: str | None = None
    client_idempotency_key: str | None = None


class RunDetail(RunSummary):
    """One stored run with the request as validated and the response as returned."""

    request: dict[str, Any]
    response: dict[str, Any]


class RunsPage(ResponseModel):
    """A page of your runs, newest first."""

    runs: list[RunSummary]
    next_before: datetime | None = Field(default=None, description="Pass as ``before`` to fetch the next page.")
