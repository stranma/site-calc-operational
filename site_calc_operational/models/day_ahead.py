"""``plan_day_ahead``: request and result."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, model_validator

from site_calc_operational.models._base import (
    RequestModel,
    ResponseModel,
    ServiceCode,
    check_battery_has_next_day_prices,
)
from site_calc_operational.models.day import Day
from site_calc_operational.models.reservation import RunInfo
from site_calc_operational.models.site import Site

PRICE_TAKER_SELL_EUR_PER_MWH = -500.0
"""Price the server attaches to sell bids: accepted at any market price."""

PRICE_TAKER_BUY_EUR_PER_MWH = 4000.0
"""Price the server attaches to buy bids: accepted at any market price."""


class ClearedReservation(RequestModel):
    """A reservation contract that cleared for day D (battery sites).

    Contracts on the same (service, block) add up. The clearing price is
    stored with the run but does not change the plan.
    """

    service: ServiceCode = Field(description="The cleared product.")
    block_index: int = Field(ge=0, le=5, description="The 4-hour block of day D.")
    volume_mw: float = Field(gt=0, description="Reserved capacity, MW.")
    clearing_price_eur_per_mw_h: float | None = Field(default=None, ge=0, description="Informational.")


class DayAheadParams(RequestModel):
    """Knobs for the day-ahead step."""

    terminal_soc_fraction: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Pin the end-of-day state of charge to this fraction of capacity; unset lets D+1 prices value it.",
    )


class PlanDayAheadRequest(RequestModel):
    """Everything the server needs to plan the day-ahead bids of day D.

    Call after the reservation results are known and before the day-ahead
    gate. Battery sites pass ``cleared_reservations``; CHP sites pass
    ``chp_pins`` (block index to MW operating point held over the block).
    """

    site: Site = Field(description="The site to plan.")
    day: Day = Field(description="The delivery day and its prices.")
    cleared_reservations: list[ClearedReservation] = Field(
        default_factory=list, description="Reservation contracts that cleared for day D (battery sites)."
    )
    chp_pins: dict[int, float] = Field(
        default_factory=dict, description="Block index -> MW operating point to hold (CHP sites)."
    )
    params: DayAheadParams = Field(default_factory=DayAheadParams, description="Planner knobs.")
    metadata: dict[str, Any] | None = Field(default=None, description="Free-form tags stored with the run.")

    @model_validator(mode="after")
    def _consistent(self) -> PlanDayAheadRequest:
        check_battery_has_next_day_prices(self.site, self.day)
        declared = self.site.declared_services()
        unknown = sorted({c.service for c in self.cleared_reservations} - declared)
        if unknown:
            raise ValueError(f"cleared_reservations name services no device declares an ability for: {unknown}")
        if self.chp_pins and not any(d.type == "chp" for d in self.site.devices):
            raise ValueError("chp_pins given but the site has no chp device")
        return self


class DayAheadBid(ResponseModel):
    """One price-taker bid for a quarter-hour of day D.

    Positive ``volume_mw`` sells at :data:`PRICE_TAKER_SELL_EUR_PER_MWH`,
    negative buys at :data:`PRICE_TAKER_BUY_EUR_PER_MWH`.
    """

    qh_index: int = Field(description="0..95, quarter-hour of day D.")
    interval_start: datetime = Field(description="Local start of the quarter-hour.")
    volume_mw: float = Field(description="Signed: positive = sell (export), negative = buy (import).")
    price_eur_per_mwh: float = Field(description="Bid price; the price-taker constants above.")


class Schedule(ResponseModel):
    """The planned dispatch over the whole horizon.

    Index ``t`` is quarter-hour ``t`` from local midnight of day D. The
    horizon is 192 entries for a battery site (day D plus the D+1 tail) and
    96 for a CHP site. Only the first ``committed_qh`` entries are bid.
    """

    storage_soc_mwh: dict[str, list[float]] = Field(
        default_factory=dict,
        description="Per-device SOC at all boundaries, including initial and terminal states (T+1).",
    )
    device_power: dict[str, dict[str, list[float]]] = Field(default_factory=dict)
    net_flow_mw: list[float] = Field(description="Export minus import per quarter-hour.")
    committed_qh: int = Field(description="Entries that belong to day D and are bid (96).")
    soc_mwh: list[float] | None = Field(default=None, description="Battery state at the START of each interval.")
    soc_min_mwh: list[float] | None = Field(default=None, description="Lower SOC envelope from the reservations.")
    soc_max_mwh: list[float] | None = Field(default=None, description="Upper SOC envelope from the reservations.")
    band_lo_mw: list[float] | None = Field(default=None, description="Lowest net flow the reservations allow.")
    band_hi_mw: list[float] | None = Field(default=None, description="Highest net flow the reservations allow.")


class DayAheadPlan(ResponseModel):
    """Result of ``plan_day_ahead``."""

    storage_soc_end_mwh: dict[str, float] = Field(
        default_factory=dict, description="Each storage device state at the end of day D."
    )
    bids: list[DayAheadBid] = Field(description="96 bids for day D, in quarter-hour order.")
    schedule: Schedule
    soc_end_mwh: float | None = Field(
        default=None,
        description=(
            "Planned battery state at midnight after day D. Use it as tomorrow's initial_soc_mwh "
            "when you have no measurement; a measured state of charge is better."
        ),
    )
    objective_eur: float = Field(description="Planned day value including market fees.")
    expected_da_value_eur: float = Field(description="Planned day-ahead value before fees.")
    market_fees_eur: float = Field(description="Fees inside the objective (zero or negative).")
    run: RunInfo
