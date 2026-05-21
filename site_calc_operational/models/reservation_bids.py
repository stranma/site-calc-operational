"""Pydantic models for the on-prem reservation-bid endpoints.

Hand-mirrored from ``server-onprem/src/site_calc_onprem/schemas.py`` to give
callers a typed alternative to the ``dict[str, Any]`` payloads accepted by
``OnPremClient.{build,evaluate}_reservation_bids`` and
``OnPremClient.most_probable_realization``.

Usage (typed -> dict on the wire):

    from site_calc_operational.models import ReservationBidPlanRequest, ReservationBidPlanResult

    req = ReservationBidPlanRequest(
        sites=[...],
        timespan=TimeSpanRequest(period_start=..., period_end=..., resolution="15min"),
        services=["afrr_plus", "afrr_minus"],
        acceptance=[
            BidAcceptanceEntry(
                service="afrr_plus",
                interval_start=...,
                distribution=LogNormalParams(mu=1.5, sigma=0.6),
            ),
            ...
        ],
    )
    raw = client.build_reservation_bids(req.model_dump(mode="json"))
    result = ReservationBidPlanResult.model_validate(raw)
    print(result.expected_revenue, result.most_probable_realization.joint_probability)

These models reproduce field names, types, and constraints from the server
verbatim; field-name parity is pinned by ``test_reservation_bid_models_drift.py``
so server-side renames break client-side tests at the next pull.

Scope is intentionally narrow: only the reservation-bid family. Existing
``OnPremClient`` methods keep their ``dict[str, Any]`` signatures so the
package is back-compatible. ``device_planning``, ``runs``, and
``optimal_bidding`` response shapes are not modelled here; their request
side is partly covered by the shared types (``TimeSpanRequest``,
``SiteRequest``, ``DeviceRequest``, ``OptimizationConfig``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from site_calc_operational.models._base import ServiceCode
from site_calc_operational.models.devices import TypedDevice

# Re-export for back-compat (callers were importing ServiceCode from here).
__all_service_code = ServiceCode


# ---------------------------------------------------------------------------
# Shared structural types -- minimal mirrors of the on-prem server's request
# leaf shapes, kept just thorough enough to construct a reservation-bid
# request without dropping back to dicts.
# ---------------------------------------------------------------------------


class TimeSpanRequest(BaseModel):
    """Optimization horizon. Mirrors ``server-onprem`` ``TimeSpanRequest``.

    For the reservation-bid endpoints the server requires a single calendar
    day starting at local-tz midnight at 15-minute resolution; the client-
    side model does not enforce that (it lets the server raise the
    descriptive ``TRANSLATION_ERROR``).
    """

    period_start: datetime
    period_end: datetime
    resolution: Literal["15min", "1h", "1hour"]


class DeviceRequest(BaseModel):
    """Device with type-specific ``properties``. Properties stay loosely typed
    because the on-prem server accepts any dict-shape recognised by the
    translator (``electricity_import``, ``chp``, ``ans_abilities``, ...).
    """

    model_config = ConfigDict(extra="allow")

    name: str = Field(..., min_length=1, max_length=100)
    type: str
    properties: dict[str, Any] = Field(default_factory=dict)
    schedule: dict[str, Any] | None = None
    ancillary_services: dict[str, Any] | None = None


class SiteRequest(BaseModel):
    """Site holding one or more devices.

    ``devices`` accepts both typed device wrappers (``CHPDevice``,
    ``BatteryDevice``, ..., dispatched by their ``type`` literal) and the
    generic ``DeviceRequest`` (any ``type`` string + ``properties`` dict).
    The typed shapes give IDE autocomplete and Pydantic validation; the
    generic shape stays available for device types not (yet) modelled and
    for forward compatibility with server-side additions. Both serialize
    to the same wire JSON so a list can mix them freely.
    """

    site_id: str = Field(..., min_length=1, max_length=100)
    devices: list[Union[TypedDevice, DeviceRequest]] = Field(..., min_length=1)
    constraints: dict[str, Any] | None = None


class OptimizationConfig(BaseModel):
    """Solver and config knobs. All fields optional with sensible defaults."""

    objective: Literal["maximize_profit", "minimize_cost", "maximize_self_consumption"] = "maximize_profit"
    time_limit_seconds: int = Field(default=300, ge=1, le=3600)
    mip_gap: float = Field(default=0.01, ge=0.0, le=0.1)
    solver: Literal["cbc", "highs", "gurobi", "cplex", "glpk"] | None = "highs"


# ---------------------------------------------------------------------------
# Acceptance distribution -- tagged union mirroring the server's
# ``AcceptanceDistributionInput``.
# ---------------------------------------------------------------------------


class LogNormalParams(BaseModel):
    """Direct ``(mu, sigma)`` parameters for a log-normal clearing-price
    distribution.

    ``ln(price_in_EUR_per_MW_per_hour) ~ Normal(mu, sigma)``. All prices on
    this wire (capacity prices, activation revenue, distribution parameters,
    block-clearing prices) are in **EUR/MW/h**.

    Common transforms:

    * Mean of the distribution (EUR/MW/h): ``exp(mu + sigma**2 / 2)``
    * Median (EUR/MW/h): ``exp(mu)``
    * Coefficient of variation: ``sqrt(exp(sigma**2) - 1)``

    Use :meth:`from_mean_cv` if you'd rather specify the mean directly
    instead of the log-space ``mu``.
    """

    type: Literal["lognormal"] = "lognormal"
    mu: float = Field(..., description="Mean of ``ln(price)`` where price is in EUR/MW/h.")
    sigma: float = Field(..., gt=0, description="Std-dev of ``ln(price)``; must be > 0. Higher = wider upper tail.")

    @classmethod
    def from_mean_cv(cls, mean: float, cv: float) -> "LogNormalParams":
        """Construct from EUR/MW/h mean and coefficient of variation.

        The reservation-bid planner thinks in EUR/MW/h, not in log-space
        ``mu``. Most callers know what mean clearing price to expect for a
        block and have a rough sense of its variability; this avoids the
        off-by-``sigma**2 / 2`` mistake of setting ``mu = ln(mean)``.

        :param mean: Expected clearing price in EUR/MW/h. Must be > 0.
        :param cv: Coefficient of variation (std-dev / mean). Typical
            day-ahead aFRR markets sit around ``cv ~= 0.5..0.8``.
            Must be > 0.
        :returns: A :class:`LogNormalParams` whose log-normal distribution
            has the requested mean and CV.
        :raises ValueError: If ``mean <= 0`` or ``cv <= 0``.
        """
        from math import log, sqrt

        if mean <= 0:
            raise ValueError(f"mean must be > 0, got {mean}")
        if cv <= 0:
            raise ValueError(f"cv must be > 0, got {cv}")
        sigma2 = log(1.0 + cv * cv)
        sigma = sqrt(sigma2)
        mu = log(mean) - sigma2 / 2.0
        return cls(mu=mu, sigma=sigma)


class LogNormalFromQuantilesParams(BaseModel):
    """Log-normal fit from ``(cdf_probability, price)`` pairs.

    Closed-form OLS fit; requires at least two pairs with distinct
    probabilities and positive prices (validated server-side).
    """

    type: Literal["lognormal_from_quantiles"] = "lognormal_from_quantiles"
    quantiles: list[tuple[float, float]] = Field(..., min_length=2)


class EmpiricalPercentilesParams(BaseModel):
    """Piecewise-linear survival distribution from ``(price, survival)``
    breakpoints.

    Flat-extrapolated outside the grid; no finite-mean guarantee. The
    reservation-bid planner can still run against it, but if the upper tail
    saturates above zero the bid-price objective may need a ``tail_cap``
    that the on-prem API does not currently expose.
    """

    type: Literal["empirical_percentiles"] = "empirical_percentiles"
    breakpoints: list[tuple[float, float]] = Field(..., min_length=2)


AcceptanceDistributionInput = Annotated[
    Union[LogNormalParams, LogNormalFromQuantilesParams, EmpiricalPercentilesParams],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Per-(service, interval) leaf shapes
# ---------------------------------------------------------------------------


class BidAcceptanceEntry(BaseModel):
    """One acceptance distribution for a ``(service, 4-hour block)`` pair."""

    service: ServiceCode
    interval_start: datetime
    distribution: AcceptanceDistributionInput


class ReservationBidIn(BaseModel):
    """A single reservation bid: which service, which block, what volume,
    what price."""

    service: ServiceCode
    interval_start: datetime
    volume_mw: float = Field(..., gt=0)
    capacity_price: float = Field(..., ge=0, description="EUR/MW/h.")


class ActivationRevenueEntry(BaseModel):
    """Expected activation revenue (EUR/MW/h) for a ``(service, block)``.

    This is the *additional* revenue the device expects to earn from being
    activated (called on to deliver the reserved capacity), on top of the
    capacity payment. The planner weights it by the per-bid clearing
    probability to compute total expected revenue under each Variant.

    Conservative default: ``0.0`` per ``(service, block)`` -- "we only get
    paid for being prequalified, no activation upside expected". Pass
    non-zero values when the operator has a forecast of activation volume
    *and* the price for activated MW.
    """

    service: ServiceCode
    interval_start: datetime
    eur_per_mw_h: float = Field(..., ge=0, description="Expected activation revenue (EUR/MW/h).")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ReservationBidPlanRequest(BaseModel):
    """Request body for ``POST /v1/reservation-bids`` (the planner).

    The on-prem server enforces:

    * Exactly one site in ``sites`` (the v1 planner targets a single site).
    * ``timespan`` must be a single calendar day starting at local-tz
      midnight, at 15-minute resolution (96 intervals).
    * Exactly one ANS-capable device on the site (declared via
      ``CHPProperties.ans_abilities`` or equivalent).
    * ``acceptance`` must cover the **Cartesian product** of
      ``services`` and the six 4-hour blocks of the day. For aFRR+ and
      aFRR- this is 12 entries (2 services x 6 blocks). Missing a single
      (service, block) entry returns 422 ``TRANSLATION_ERROR``.
    * Each ``acceptance`` entry's ``interval_start`` must land on a 4-hour
      block boundary: ``00:00``, ``04:00``, ``08:00``, ``12:00``, ``16:00``,
      ``20:00`` of the target day. See
      :func:`site_calc_operational.models.four_hour_block_starts`.
    """

    sites: list[SiteRequest] = Field(
        ..., min_length=1, max_length=1, description="Exactly one site (v1 planner constraint)."
    )
    timespan: TimeSpanRequest = Field(..., description="Single calendar day, local-tz midnight, 15-minute resolution.")
    services: list[ServiceCode] = Field(
        ..., min_length=1, description="ANS services to bid into, e.g. ``['afrr_plus', 'afrr_minus']``."
    )
    acceptance: list[BidAcceptanceEntry] = Field(
        ...,
        min_length=1,
        description=(
            "Acceptance distribution per ``(service, 4-hour block)`` pair. Must cover the "
            "full Cartesian product of ``services`` and the six 4-hour blocks of the day "
            "(12 entries for 2 services). Missing a single combination returns 422."
        ),
    )
    expected_activation_revenue: list[ActivationRevenueEntry] = Field(
        default_factory=list,
        description=(
            "Per-(service, block) expected EUR/MW/h from activation, **on top of** the "
            "capacity payment. Defaults to empty (no activation upside)."
        ),
    )
    assume_maximal: bool = Field(
        default=False,
        description=(
            "If True, restrict Pass 2 to maximal-feasible Variants only (the planner skips "
            "any Variant that's a strict subset of a feasible one). Faster (~1.5-3.5x on "
            "measured fixtures) but unsafe when the acceptance distribution's upper tail "
            "is bounded -- can prune the true optimum. The planner emits a "
            "``diagnostics.winner_is_maximal`` flag so you can verify per-run whether the "
            "preconditions held; flip this to True only after observing "
            "``winner_is_maximal=True`` consistently. Default False (exhaustive)."
        ),
    )
    optimization_config: OptimizationConfig | None = None
    metadata: dict[str, Any] | None = None


class ReservationBidEvaluateRequest(BaseModel):
    """Request body for ``POST /v1/reservation-bids/evaluate``."""

    sites: list[SiteRequest] = Field(..., min_length=1, max_length=1)
    timespan: TimeSpanRequest
    bids: list[ReservationBidIn] = Field(..., min_length=1)
    acceptance: list[BidAcceptanceEntry] = Field(..., min_length=1)
    expected_activation_revenue: list[ActivationRevenueEntry] = Field(default_factory=list)
    optimization_config: OptimizationConfig | None = None
    metadata: dict[str, Any] | None = None


class ReservationBidMPRRequest(BaseModel):
    """Request body for ``POST /v1/reservation-bids/most-probable-realization``."""

    sites: list[SiteRequest] = Field(..., min_length=1, max_length=1)
    timespan: TimeSpanRequest
    bids: list[ReservationBidIn] = Field(..., min_length=1)
    acceptance: list[BidAcceptanceEntry] = Field(..., min_length=1)
    optimization_config: OptimizationConfig | None = None
    metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ReservationBidOut(BaseModel):
    """A single bid in a planner response or MPR ``contracts`` list."""

    service: ServiceCode
    interval_start: datetime
    volume_mw: float
    capacity_price: float


class EvaluationResult(BaseModel):
    """The ``evaluation`` block inside a planner response, and the standalone
    response body for ``/v1/reservation-bids/evaluate``."""

    expected_revenue: float


class MostProbableRealizationResult(BaseModel):
    """The ``most_probable_realization`` block inside a planner response, and
    the standalone response body for
    ``/v1/reservation-bids/most-probable-realization``.
    """

    contracts: list[ReservationBidOut]
    baseline_da: float
    realized_revenue: float
    joint_probability: float = Field(..., ge=0.0, le=1.0)


class ReservationBidPlanResult(BaseModel):
    """Full response body for ``POST /v1/reservation-bids``.

    Bundles the planner's chosen bids, its expected revenue under those bids,
    the per-run ``diagnostics`` (winner_is_maximal, variant counts, ...),
    the planner's own most-probable realization of its own plan, and a
    cross-check evaluation. The ``diagnostics`` keys are documented in
    ``site_calc.planning.reservation_bids.build_reservation_bids``; they are
    kept loosely typed here because the set evolves with the planner.
    """

    bids: list[ReservationBidOut]
    expected_revenue: float
    diagnostics: dict[str, Any]
    most_probable_realization: MostProbableRealizationResult
    evaluation: EvaluationResult


__all__ = [
    "ServiceCode",
    "TimeSpanRequest",
    "DeviceRequest",
    "SiteRequest",
    "OptimizationConfig",
    "LogNormalParams",
    "LogNormalFromQuantilesParams",
    "EmpiricalPercentilesParams",
    "AcceptanceDistributionInput",
    "BidAcceptanceEntry",
    "ReservationBidIn",
    "ActivationRevenueEntry",
    "ReservationBidPlanRequest",
    "ReservationBidEvaluateRequest",
    "ReservationBidMPRRequest",
    "ReservationBidOut",
    "EvaluationResult",
    "MostProbableRealizationResult",
    "ReservationBidPlanResult",
]
