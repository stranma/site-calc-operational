"""Typed Pydantic models for the on-prem reservation-bid endpoints.

Pure additive layer over the existing ``OnPremClient`` methods, which still
take and return ``dict[str, Any]``. Construct a typed request, ``model_dump``
it on the way out, and ``model_validate`` the response on the way back in:

    from site_calc_operational.models import (
        ReservationBidPlanRequest,
        ReservationBidPlanResult,
    )

    req = ReservationBidPlanRequest(...)
    raw = client.build_reservation_bids(req.model_dump(mode="json"))
    result = ReservationBidPlanResult.model_validate(raw)

Other endpoints (``device_planning``, ``runs``, ``optimal_bidding``) are not
modelled yet -- scope of v0.2.1 is reservation-bid only.
"""

from site_calc_operational.models.devices import (
    ANSAbility,
    BatteryDevice,
    BatteryProperties,
    CHPDevice,
    CHPProperties,
    ElectricityExportDevice,
    ElectricityExportProperties,
    ElectricityImportDevice,
    ElectricityImportProperties,
    GasImportDevice,
    GasImportProperties,
    HeatAccumulatorDevice,
    HeatAccumulatorProperties,
    HeatDemandDevice,
    HeatDemandProperties,
    HeatExportDevice,
    HeatExportProperties,
    Profile,
    TypedDevice,
)
from site_calc_operational.models.reservation_bids import (
    AcceptanceDistributionInput,
    ActivationRevenueEntry,
    BidAcceptanceEntry,
    DeviceRequest,
    EmpiricalPercentilesParams,
    EvaluationResult,
    LogNormalFromQuantilesParams,
    LogNormalParams,
    MostProbableRealizationResult,
    OptimizationConfig,
    ReservationBidEvaluateRequest,
    ReservationBidIn,
    ReservationBidMPRRequest,
    ReservationBidOut,
    ReservationBidPlanRequest,
    ReservationBidPlanResult,
    ServiceCode,
    SiteRequest,
    TimeSpanRequest,
)

__all__ = [
    # Reservation-bid request/response models
    "AcceptanceDistributionInput",
    "ActivationRevenueEntry",
    "BidAcceptanceEntry",
    "EmpiricalPercentilesParams",
    "EvaluationResult",
    "LogNormalFromQuantilesParams",
    "LogNormalParams",
    "MostProbableRealizationResult",
    "ReservationBidEvaluateRequest",
    "ReservationBidIn",
    "ReservationBidMPRRequest",
    "ReservationBidOut",
    "ReservationBidPlanRequest",
    "ReservationBidPlanResult",
    # Shared request types
    "DeviceRequest",
    "OptimizationConfig",
    "ServiceCode",
    "SiteRequest",
    "TimeSpanRequest",
    # Typed device properties (per-type)
    "ANSAbility",
    "Profile",
    "BatteryProperties",
    "CHPProperties",
    "ElectricityExportProperties",
    "ElectricityImportProperties",
    "GasImportProperties",
    "HeatAccumulatorProperties",
    "HeatDemandProperties",
    "HeatExportProperties",
    # Typed devices (tagged-union members)
    "BatteryDevice",
    "CHPDevice",
    "ElectricityExportDevice",
    "ElectricityImportDevice",
    "GasImportDevice",
    "HeatAccumulatorDevice",
    "HeatDemandDevice",
    "HeatExportDevice",
    "TypedDevice",
]
