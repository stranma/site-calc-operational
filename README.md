# Site-Calc Operational Client

Python client for Site-Calc operational optimization API - day-ahead bidding and short-term dispatch with ancillary services.

## Installation

> **Note:** the package is not yet published to PyPI. Install from the GitHub source until the first PyPI release.

```bash
# From source (recommended while v1.x is unreleased on PyPI):
pip install git+https://github.com/stranma/site-calc-operational.git

# Or, for local development inside this repo:
git clone https://github.com/stranma/site-calc-operational.git
cd site-calc-operational
pip install -e ".[dev]"
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

## MCP server (LLM-driven scenario building)

The package ships an optional [Model Context Protocol](https://modelcontextprotocol.io/) server that exposes 17 tools for building and submitting operational device-planning scenarios from an LLM (Claude Desktop, ChatGPT, Cursor, etc.). It wraps `OnPremClient` and runs locally on the user's machine, so the LLM can persist generated profile data to disk and reference it in subsequent tool calls.

### Install

```bash
# From source with the MCP extra:
pip install "site-calc-operational[mcp] @ git+https://github.com/stranma/site-calc-operational.git"

# Or in this checkout:
pip install -e ".[mcp]"
```

The extra pulls `fastmcp>=2.0`. Once installed, the package exposes both a console script (`site-calc-operational-mcp`) and a module entry point (`python -m site_calc_operational.mcp`).

### Configure the MCP client

Set environment variables so the server can reach your on-prem deployment:

```bash
export SITE_CALC_OPERATIONAL_API_URL="http://localhost:8000"   # or your on-prem URL
export SITE_CALC_OPERATIONAL_API_KEY="op_..."                  # mint with: site-calc-op create-user
export SITE_CALC_OPERATIONAL_DATA_DIR="$HOME/.site-calc/data"  # optional; for save_data_file output
```

Then register the server with your MCP-aware client. For Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "site-calc-operational": {
      "command": "site-calc-operational-mcp",
      "env": {
        "SITE_CALC_OPERATIONAL_API_URL": "http://localhost:8000",
        "SITE_CALC_OPERATIONAL_API_KEY": "op_..."
      }
    }
  }
}
```

If your environment requires `python -m`, swap `command` for `python` and add `"args": ["-m", "site_calc_operational.mcp"]`.

### Tools exposed

| Category | Tools |
|----------|-------|
| Server info | `health`, `get_version` |
| Scenario assembly | `create_scenario`, `add_device`, `remove_device`, `set_timespan`, `set_optimization_config`, `review_scenario`, `delete_scenario`, `list_scenarios` |
| Solving | `solve` (synchronous), `cancel_active` |
| Run inspection | `get_run`, `list_runs` |
| Schema / data | `get_device_schema`, `save_data_file`, `fetch_url` |

### Workflow

The LLM is expected to follow this loop:

1. `create_scenario(name, site_id)` – starts a fresh draft.
2. `add_device(...)` – one call per device. Use `get_device_schema(device_type)` first to discover required properties.
3. `set_timespan(period_start, period_end, resolution)` – ISO datetimes; resolution is `"15min" | "30min" | "1h"`.
4. (optional) `set_optimization_config(...)` – override solver / objective defaults.
5. `review_scenario(scenario_id)` – inspect `validation.valid`; fix any reported errors before submitting.
6. `solve(scenario_id, idempotency_key=...)` – synchronous solve; returns `run_id` plus the server-side summary.
7. `get_run(run_id)` – fetch full per-device schedules from the on-prem server when needed.

Profile-shaped properties (`price`, `demand_profile`, `generation_profile`, …) accept three forms:

- a scalar (broadcast to the timespan), e.g. `"price": 50.0`;
- an explicit list (validated against the interval count), e.g. `"price": [55, 50, 48, ...]`;
- a file reference, e.g. `"price": {"file": "C:/data/prices.csv", "column": "price_eur_mwh"}`.

`save_data_file` and `fetch_url` exist so the LLM can persist arrays it has generated, or download remote CSVs, and then reference them in the file form above.

## License

MIT License

## Support

- Issues: https://github.com/site-calc/operational-client/issues
- Documentation: https://docs.site-calc.example.com
