"""``plan_reservation``: request and result."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, model_validator

from site_calc_operational.models._base import (
    BLOCKS_PER_DAY,
    RequestModel,
    ResponseModel,
    ServiceCode,
    check_battery_has_next_day_prices,
)
from site_calc_operational.models.day import AnsForecastEntry, Day, ReservationParams
from site_calc_operational.models.site import Site


class PlanReservationRequest(RequestModel):
    """Everything the server needs to plan the reservation bids of day D.

    Call before the reservation gate on the planning day. ``ans_forecast``
    must contain one entry for every (service, block) of the requested
    ``services``: six blocks per service.
    """

    site: Site = Field(description="The site to plan.")
    day: Day = Field(description="The delivery day and its prices.")
    services: list[ServiceCode] = Field(min_length=1, description="Products to bid; each needs a full forecast.")
    ans_forecast: list[AnsForecastEntry] = Field(
        min_length=1, description="Acceptance forecast per (service, block) for every requested service."
    )
    params: ReservationParams = Field(default_factory=ReservationParams, description="Planner knobs.")
    metadata: dict[str, Any] | None = Field(default=None, description="Free-form tags stored with the run.")

    @model_validator(mode="after")
    def _coverage(self) -> PlanReservationRequest:
        check_battery_has_next_day_prices(self.site, self.day)
        undeclared = sorted(set(self.services) - self.site.declared_services())
        if undeclared:
            raise ValueError(f"services {undeclared} are not in any device's ans_abilities")
        seen = {(e.service, e.block_index) for e in self.ans_forecast}
        if len(seen) != len(self.ans_forecast):
            raise ValueError("ans_forecast has duplicate (service, block_index) entries")
        missing = [(s, b) for s in dict.fromkeys(self.services) for b in range(BLOCKS_PER_DAY) if (s, b) not in seen]
        if missing:
            raise ValueError(f"ans_forecast is missing entries for {missing[:6]}")
        return self


class ReservationBid(ResponseModel):
    """One reservation bid (or, in ``most_probable_realization``, one assumed contract)."""

    service: ServiceCode
    block_index: int = Field(description="0..5, the 4-hour block of day D.")
    interval_start: datetime = Field(description="Local start of the block.")
    volume_mw: float = Field(description="Offered capacity, MW.")
    capacity_price_eur_per_mw_h: float = Field(description="Asking capacity price, EUR per MW and hour.")
    forecast_clear_probability: float | None = Field(
        default=None, description="Probability the bid clears under the forecast; absent on assumed contracts."
    )


class ExpectedRevenue(ResponseModel):
    """Expected revenue of the bid set over the acceptance forecast, EUR for the day."""

    reservation: float = Field(description="Expected capacity payments.")
    activation: float = Field(description="Expected activation revenue.")
    da_arbitrage: float = Field(description="Expected day-ahead trading value given the reservations.")
    total: float = Field(description="Sum of the three.")


class MostProbableRealization(ResponseModel):
    """The single most likely clearing outcome and what it would earn."""

    contracts: list[ReservationBid] = Field(description="Bids assumed to clear in the most probable outcome.")
    baseline_da: float = Field(description="Day-ahead value with no reservation at all, EUR.")
    realized_revenue: float = Field(description="Revenue of the day under this outcome, EUR.")
    joint_probability: float = Field(description="Probability of exactly this outcome.")


class RunInfo(ResponseModel):
    """Provenance of a plan: what produced it and how long it took."""

    site_calc_version: str
    site_calc_commit_sha: str
    planner: str
    params: dict[str, Any]
    horizon_qh: int = Field(description="Quarter-hours the planner dispatched over (192 with a D+1 tail, else 96).")
    solve_seconds: float


class ReservationPlan(ResponseModel):
    """Result of ``plan_reservation``."""

    bids: list[ReservationBid] = Field(description="Bids to submit; blocks without a bid are not offered.")
    expected_revenue: ExpectedRevenue
    most_probable_realization: MostProbableRealization
    diagnostics: dict[str, Any] = Field(default_factory=dict, description="Planner-specific details.")
    run: RunInfo
