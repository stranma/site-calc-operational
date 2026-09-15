"""The delivery day: prices, the reservation acceptance forecast, planner knobs."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from pydantic import Field, model_validator

from site_calc_operational.models._base import RequestModel, ServiceCode


class Day(RequestModel):
    """The delivery day D and its day-ahead prices.

    Prices are EUR/MWh per quarter-hour in the site's local time, starting at
    local midnight. A battery site must also send the prices of the next day
    (D+1): the planner dispatches over both days so the state of charge at
    midnight is valued, while only day D is committed. Days on which the
    clocks change cannot be planned; for a battery site neither can the day
    before one.
    """

    date: dt.date = Field(description="The delivery day D.")
    tz: str = Field(min_length=1, description="IANA timezone of the site, e.g. Europe/Prague.")
    da_price_eur_per_mwh_d: list[float] = Field(
        min_length=96, max_length=96, description="96 day-ahead prices for day D, one per quarter-hour."
    )
    da_price_eur_per_mwh_d1: list[float] | None = Field(
        default=None,
        min_length=96,
        max_length=96,
        description="96 day-ahead prices (or your forecast) for D+1. Required for a battery site.",
    )


class LogNormal(RequestModel):
    """Clearing price is log-normal: ``ln(price) ~ Normal(mu, sigma)``, price in EUR/MW/h."""

    type: Literal["lognormal"] = "lognormal"
    mu: float = Field(description="Mean of ln(price).")
    sigma: float = Field(gt=0, description="Standard deviation of ln(price).")


class LogNormalFromQuantiles(RequestModel):
    """Log-normal fitted to ``(probability, price)`` pairs.

    Each pair reads "the clearing price is below ``price`` with ``probability``".
    Give at least two pairs with distinct probabilities. Note the order:
    probability first, price second (the opposite of :class:`EmpiricalPercentiles`).
    """

    type: Literal["lognormal_from_quantiles"] = "lognormal_from_quantiles"
    quantiles: list[tuple[float, float]] = Field(
        min_length=2, description="Pairs (probability in (0, 1), price EUR/MW/h) the fitted curve must pass through."
    )

    @model_validator(mode="after")
    def _shape(self) -> LogNormalFromQuantiles:
        probs = [p for p, _ in self.quantiles]
        if any(not 0.0 < p < 1.0 for p in probs):
            raise ValueError("quantiles: every probability must be strictly between 0 and 1")
        if len(set(probs)) != len(probs):
            raise ValueError("quantiles: probabilities must be distinct")
        if any(x <= 0.0 for _, x in self.quantiles):
            raise ValueError("quantiles: prices must be positive")
        return self


class EmpiricalPercentiles(RequestModel):
    """Piecewise-linear curve of "a bid at ``price`` clears with ``probability``", prices ascending.

    Note the order: price first, probability second (the opposite of
    :class:`LogNormalFromQuantiles`).
    """

    type: Literal["empirical_percentiles"] = "empirical_percentiles"
    breakpoints: list[tuple[float, float]] = Field(
        min_length=2, description="Pairs (price EUR/MW/h, probability of clearing at that price), price ascending."
    )

    @model_validator(mode="after")
    def _shape(self) -> EmpiricalPercentiles:
        prices = [x for x, _ in self.breakpoints]
        if any(b <= a for a, b in zip(prices, prices[1:], strict=False)):
            raise ValueError("breakpoints: prices must be strictly ascending")
        if any(not 0.0 <= p <= 1.0 for _, p in self.breakpoints):
            raise ValueError("breakpoints: every probability must be between 0 and 1")
        return self


Distribution = Annotated[
    LogNormal | LogNormalFromQuantiles | EmpiricalPercentiles,
    Field(discriminator="type"),
]
"""How likely a reservation bid is to clear as a function of its price."""


class AnsForecastEntry(RequestModel):
    """Acceptance forecast for one service on one 4-hour block of day D."""

    service: ServiceCode = Field(description="The product this entry describes.")
    block_index: int = Field(ge=0, le=5, description="0 = 00-04, 1 = 04-08, ... 5 = 20-24 local time.")
    distribution: Distribution = Field(description="Clearing-price distribution for this service and block.")
    expected_activation_eur_per_mw_h: float = Field(
        default=0.0,
        ge=0,
        description="Expected activation revenue per reserved MW and hour on top of the capacity payment; 0 if unknown",
    )


class ReservationParams(RequestModel):
    """Planner knobs for the reservation step.

    ``planner="sitecalc"`` optimises reservation and day-ahead together and is
    the recommended setting; with two services it takes around ten minutes.
    ``planner="baseline"`` prices each block from your acceptance forecast at
    ``px_percentile`` (never below what the day-ahead market would pay for
    the same capacity), in seconds.
    """

    planner: Literal["sitecalc", "baseline"] = Field(default="sitecalc", description="Which planner to run.")
    assume_maximal: bool = Field(
        default=False,
        description=(
            "Faster variant of the sitecalc planner that only considers full-volume bids. "
            "Use it once you have seen it choose the same bids as the default on comparable days."
        ),
    )
    px_percentile: float = Field(
        default=0.5, gt=0, lt=1, description="Acceptance quantile the baseline planner prices at."
    )
    terminal_soc_fraction: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Pin the end-of-day state of charge to this fraction of capacity; unset lets D+1 prices value it.",
    )
