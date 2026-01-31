"""Site-Calc Operational Client

Python client for day-ahead bidding and short-term dispatch optimization with ancillary services.
"""

from site_calc_operational.api.client import OperationalClient

__version__ = "1.1.0"

__all__ = [
    "OperationalClient",
    "__version__",
]

# Note: Model imports are commented out until the models module is fully implemented.
# Once implemented, uncomment the following:
#
# from site_calc_operational.models import (
#     # Core models
#     TimeSpan,
#     Resolution,
#     Location,
#     # Device models
#     Battery,
#     CHP,
#     HeatAccumulator,
#     Photovoltaic,
#     HeatDemand,
#     ElectricityDemand,
#     ElectricityImport,
#     ElectricityExport,
#     GasImport,
#     HeatExport,
#     # Site and configuration
#     Site,
#     Schedule,
#     AncillaryServices,
#     MarketForecasts,
#     OpportunityCosts,
#     LockedReservations,
#     OptimizationConfig,
#     # Request models
#     OptimalBiddingRequest,
#     DevicePlanningRequest,
#     # Response models
#     Job,
#     OptimalBiddingResponse,
#     DevicePlanningResponse,
# )
