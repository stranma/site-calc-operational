"""Site-Calc Operational Client

Python client for day-ahead bidding and short-term dispatch optimization with ancillary services.
"""

from site_calc_operational.api.client import OperationalClient
from site_calc_operational.models import (
    # Core models
    TimeSpan,
    Resolution,
    Location,
    # Device models
    Battery,
    CHP,
    HeatAccumulator,
    Photovoltaic,
    HeatDemand,
    ElectricityDemand,
    ElectricityImport,
    ElectricityExport,
    GasImport,
    HeatExport,
    # Site and configuration
    Site,
    Schedule,
    AncillaryServices,
    MarketForecasts,
    OpportunityCosts,
    LockedReservations,
    OptimizationConfig,
    # Request models
    OptimalBiddingRequest,
    DevicePlanningRequest,
    # Response models
    Job,
    OptimalBiddingResponse,
    DevicePlanningResponse,
)

__version__ = "1.0.0"
__all__ = [
    # Client
    "OperationalClient",
    # Core
    "TimeSpan",
    "Resolution",
    "Location",
    # Devices
    "Battery",
    "CHP",
    "HeatAccumulator",
    "Photovoltaic",
    "HeatDemand",
    "ElectricityDemand",
    "ElectricityImport",
    "ElectricityExport",
    "GasImport",
    "HeatExport",
    # Configuration
    "Site",
    "Schedule",
    "AncillaryServices",
    "MarketForecasts",
    "OpportunityCosts",
    "LockedReservations",
    "OptimizationConfig",
    # Requests/Responses
    "OptimalBiddingRequest",
    "DevicePlanningRequest",
    "Job",
    "OptimalBiddingResponse",
    "DevicePlanningResponse",
]
