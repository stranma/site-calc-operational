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
package is back-compatible (this is a 0.2.x patch, not a 0.3.0 minor).
``device_planning``, ``runs``, and ``optimal_bidding`` shapes are not modelled
in this release.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Service codes
# ---------------------------------------------------------------------------

# Lowercase wire codes; mirror site_calc.domain.ans.AncillaryService.code.
ServiceCode = Literal["afrr_plus", "afrr_minus", "mfrr_plus", "mfrr_minus"]


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
    """Site holding one or more devices."""

    site_id: str = Field(..., min_length=1, max_length=100)
    devices: list[DeviceRequest] = Field(..., min_length=1)
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
    distribution. ``ln(price) ~ Normal(mu, sigma)``."""

    type: Literal["lognormal"] = "lognormal"
    mu: float = Field(..., description="Mean of ln(price).")
    sigma: float = Field(..., gt=0, description="Std-dev of ln(price); must be > 0.")


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
    """Expected activation revenue (EUR/MW/h) for a ``(service, block)``."""

    service: ServiceCode
    interval_start: datetime
    eur_per_mw_h: float = Field(..., ge=0)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ReservationBidPlanRequest(BaseModel):
    """Request body for ``POST /v1/reservation-bids`` (the planner)."""

    sites: list[SiteRequest] = Field(..., min_length=1, max_length=1)
    timespan: TimeSpanRequest
    services: list[ServiceCode] = Field(..., min_length=1)
    acceptance: list[BidAcceptanceEntry] = Field(..., min_length=1)
    expected_activation_revenue: list[ActivationRevenueEntry] = Field(default_factory=list)
    assume_maximal: bool = False
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
