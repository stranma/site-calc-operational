# Site-Calc Operational Client

Python client for Site-Calc operational optimization API - day-ahead bidding and short-term dispatch with ancillary services.

## Installation

```bash
pip install site-calc-operational
```

## Quick Start

```python
from datetime import date
from site_calc_operational import OperationalClient
from site_calc_operational.models import (
    TimeSpan, Resolution, Site, Battery, ElectricityImport,
    OptimalBiddingRequest, MarketForecasts, OptimizationConfig
)

# Initialize client
client = OperationalClient(
    base_url="https://api.site-calc.example.com",
    api_key="op_your_api_key_here"
)

# Create 1-day optimization at 15-minute resolution
timespan = TimeSpan.for_day(date(2025, 11, 6), Resolution.MINUTES_15)

# Define devices
battery = Battery(
    name="Battery1",
    properties={
        "capacity": 10.0,
        "max_power": 5.0,
        "efficiency": 0.90,
        "initial_soc": 0.5
    },
    ancillary_services={
        "afrr_plus": {
            "can_provide": [1, 1, 1, 1, 1, 1],
            "expected_activation_profit": [80.0, 85.0, 88.0, 90.0, 87.0, 82.0]
        }
    }
)

grid_import = ElectricityImport(
    name="GridImport",
    properties={"price": [30.0]*96, "max_import": 8.0}
)

site = Site(site_id="my_site", devices=[battery, grid_import])

# Market forecasts
forecasts = MarketForecasts(
    ancillary_services={
        "afrr_plus": {
            "max_accepted_price_forecast": [15.0, 18.0, 22.0, 25.0, 23.0, 16.0],
            "period_duration_hours": 4
        }
    }
)

# Create and submit optimization request
request = OptimalBiddingRequest(
    sites=[site],
    timespan=timespan,
    market_forecasts=forecasts,
    optimization_config=OptimizationConfig(max_bid_steps=10)
)

job = client.create_optimal_bidding_job(request)
result = client.wait_for_completion(job.job_id, timeout=600)

# Display results
print(f"Total Profit: €{result.summary.expected_profit:,.2f}")
print(f"DA Revenue: €{result.summary.total_da_revenue:,.2f}")
print(f"ANS Revenue: €{result.summary.total_ancillary_revenue:,.2f}")
```

## Features

- ✅ Day-ahead market bidding optimization
- ✅ Ancillary services (aFRR, mFRR) optimization
- ✅ Multi-step bid curve generation
- ✅ Device scheduling with constraints
- ✅ 15-minute or 1-hour resolution
- ✅ Multi-site optimization
- ✅ Type-safe Pydantic models
- ✅ Automatic retry and error handling

## Capabilities

| Feature | Value |
|---------|-------|
| Max Horizon | 296 intervals (~3 days at 15-min) |
| Resolution | 15-minute or 1-hour |
| ANS Support | Yes (aFRR±, mFRR±) |
| Binary Variables | Yes (CHP on/off) |
| Timeout | 300 seconds |

## Supported Devices

- Battery (with ANS capabilities)
- CHP - Combined Heat and Power (with ANS capabilities)
- Heat Accumulator
- Photovoltaic
- Heat Demand
- Electricity Demand
- Electricity Import/Export (market interface)
- Gas Import (market interface)
- Heat Export (market interface)

## Documentation

Full documentation available at: https://docs.site-calc.example.com/operational-client

## Examples

See `examples/` directory for complete examples:
- `optimal_bidding.py` - Complete optimal bidding workflow
- `device_planning.py` - Device scheduling with locked ANS reservations
- `multi_site.py` - Multi-site optimization

## Requirements

- Python ≥ 3.10
- API key with `op_` prefix (operational client)

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Format code
ruff format .

# Type check
mypy site_calc_operational
```

## On-prem (sync) client

For self-hosted deployments running `server-onprem`:

```python
from site_calc_operational import OnPremClient

with OnPremClient(base_url="https://onprem.example.com", api_key="op_...") as client:
    health = client.health()
    print(health.site_calc_version)
    result = client.device_planning(request_payload)
    print(result["summary"])
```

The on-prem client is sync (no polling). For the SaaS server, keep using `OperationalClient` (async).

## License

MIT License

## Support

- Issues: https://github.com/site-calc/operational-client/issues
- Documentation: https://docs.site-calc.example.com
