# Site-Calc Operational Client

Python client for the Site-Calc operational optimization API -- day-ahead bidding, short-term dispatch, and ancillary-services (aFRR/mFRR) reservation-bid planning.

The package ships two HTTP clients:

| Client | Server | Pattern | Status |
|--------|--------|---------|--------|
| **`OnPremClient`** | Self-hosted `server-onprem` | Synchronous (one HTTP call blocks until the solve completes) | **Primary** -- typed models for the reservation-bid family |
| `OperationalClient` | SaaS REST API | Async submit-then-poll | Legacy; kept for existing callers |

This README focuses on the on-prem reservation-bid flow added in v0.3.0. For the legacy SaaS workflow see the bottom section.

## Installation

> **Note:** the package is not yet published to PyPI. Install from source.

```bash
# From source:
pip install git+https://github.com/stranma/site-calc-operational.git

# With the MCP-server extra (Claude Desktop / Cursor / ChatGPT integration):
pip install "site-calc-operational[mcp] @ git+https://github.com/stranma/site-calc-operational.git"

# Local development:
git clone https://github.com/stranma/site-calc-operational.git
cd site-calc-operational
pip install -e ".[dev]"
```

## Quick Start -- reservation-bid plan against an on-prem server

```python
"""Submit a day-ahead reservation-bid plan for a binary CHP unit
prequalified for aFRR+ and aFRR-, against a self-hosted server-onprem
deployment. Uses the typed Pydantic models added in v0.3.0."""

import os
from datetime import datetime, timedelta, timezone

from site_calc_operational import OnPremClient
from site_calc_operational.models import (
    ANSAbility,
    CHPDevice,
    CHPProperties,
    ElectricityExportDevice,
    ElectricityExportProperties,
    GasImportDevice,
    GasImportProperties,
    HeatExportDevice,
    HeatExportProperties,
    LogNormalParams,
    ReservationBidPlanRequest,
    ReservationBidPlanResult,
    SiteRequest,
    TimeSpanRequest,
    build_uniform_acceptance,
    build_zero_activation_revenue,
)

# 1. Timespan: one calendar day at 15-minute resolution, local-tz midnight.
#    The on-prem reservation-bid planner enforces this shape.
prague_offset = timezone(timedelta(hours=2))  # use ZoneInfo("Europe/Prague") if tzdata is available
period_start = datetime(2026, 5, 21, 0, 0, tzinfo=prague_offset)
timespan = TimeSpanRequest(
    period_start=period_start,
    period_end=period_start + timedelta(days=1),
    resolution="15min",
)

# 2. Site: one ANS-capable device (CHP with declared aFRR+/- abilities)
#    plus the supporting market interfaces the planner needs to close
#    energy balances. SiteRequest.devices accepts typed wrappers directly.
PRICE_SAMPLES = 96  # 24h * 4 intervals/h

site = SiteRequest(
    site_id="cz-chp-1",
    devices=[
        CHPDevice(
            name="CHP-bin",
            properties=CHPProperties(
                gas_input=2.5, el_output=1.0, heat_output=1.0,
                is_binary=True, max_starts_per_day=4,
                ans_abilities=[
                    ANSAbility(service="afrr_plus", min_device_power_rate=0.0, max_device_power_rate=1.0),
                    ANSAbility(service="afrr_minus", min_device_power_rate=0.0, max_device_power_rate=1.0),
                ],
            ),
        ),
        GasImportDevice(
            name="Gas",
            properties=GasImportProperties(price=[35.0] * PRICE_SAMPLES, max_import=2.5),
        ),
        ElectricityExportDevice(
            name="ElExport",
            properties=ElectricityExportProperties(price=[100.0] * PRICE_SAMPLES, max_export=1.0),
        ),
        HeatExportDevice(
            name="HeatExport",
            properties=HeatExportProperties(price=[5.0] * PRICE_SAMPLES, max_export=1.0),
        ),
    ],
)

# 3. Acceptance distribution: one entry per (service, 4-hour block).
#    build_uniform_acceptance fills the full Cartesian product (12 entries
#    for 2 services x 6 blocks). LogNormalParams.from_mean_cv lets you
#    specify the expected clearing price (EUR/MW/h) and CV directly,
#    instead of the log-space mu.
acceptance = build_uniform_acceptance(
    timespan=timespan,
    services=["afrr_plus", "afrr_minus"],
    distribution=LogNormalParams.from_mean_cv(mean=8.0, cv=0.6),  # EUR/MW/h
)

# 4. Assemble request and submit.
request = ReservationBidPlanRequest(
    sites=[site],
    timespan=timespan,
    services=["afrr_plus", "afrr_minus"],
    acceptance=acceptance,
    expected_activation_revenue=build_zero_activation_revenue(
        timespan=timespan, services=["afrr_plus", "afrr_minus"],
    ),
)

with OnPremClient(
    base_url="https://operational.algoenergy.cz",
    api_key=os.environ["ONPREM_API_KEY"],
    timeout_seconds=600.0,
) as client:
    raw = client.build_reservation_bids(
        request.model_dump(mode="json"),
        idempotency_key="rb-2026-05-21-v1",
    )

# 5. Parse the response into a typed result.
result = ReservationBidPlanResult.model_validate(raw)
print(f"Expected revenue: {result.expected_revenue:.2f} EUR")
print(f"winner_is_maximal: {result.diagnostics['winner_is_maximal']}")
for bid in result.bids:
    print(f"  {bid.service:<10}  {bid.interval_start}  vol={bid.volume_mw} MW  price={bid.capacity_price:.2f} EUR/MW/h")
mpr = result.most_probable_realization
print(f"\nMost-probable realization: {len(mpr.contracts)} contracts, realized {mpr.realized_revenue:.2f} EUR, P(joint)={mpr.joint_probability:.3f}")
```

### Reservation-bid endpoints

`OnPremClient` wraps three endpoints from `server-onprem` v0.2+:

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `build_reservation_bids(request)` | `POST /v1/reservation-bids` | Run the planner. Returns the chosen bids + expected revenue + the planner's own most-probable realization + a re-evaluation cross-check, all in one round-trip. |
| `evaluate_reservation_bids(request)` | `POST /v1/reservation-bids/evaluate` | Score a caller-supplied bid set (`expected_plan_revenue` with no search). Useful for re-checking a plan against an alternative acceptance distribution. |
| `most_probable_realization(request)` | `POST /v1/reservation-bids/most-probable-realization` | For a caller-supplied plan, return the contracts that clear at >=50% probability + the day-ahead baseline + joint probability. |

All three accept `idempotency_key` for safe retries (24-hour replay window on the server) and respect the same `BackoffPolicy` for 503 backpressure.

### Typed models layer

`site_calc_operational.models` is **additive** -- the existing client methods still accept and return `dict[str, Any]`. You can adopt the typed layer incrementally:

```python
# Loose (still works):
raw_request = {"sites": [...], "timespan": {...}, "services": [...], ...}
client.build_reservation_bids(raw_request)

# Typed:
typed_request = ReservationBidPlanRequest(sites=[...], timespan=..., services=[...], ...)
client.build_reservation_bids(typed_request.model_dump(mode="json"))
```

Reservation-bid model surface (see `models/__init__.py` for the full export list):

- **Request bodies**: `ReservationBidPlanRequest`, `ReservationBidEvaluateRequest`, `ReservationBidMPRRequest`
- **Response bodies**: `ReservationBidPlanResult`, `EvaluationResult`, `MostProbableRealizationResult`
- **Acceptance distribution** (discriminated union by `type`): `LogNormalParams`, `LogNormalFromQuantilesParams`, `EmpiricalPercentilesParams`
- **Leaves**: `BidAcceptanceEntry`, `ReservationBidIn`, `ReservationBidOut`, `ActivationRevenueEntry`, `ANSAbility`
- **Typed devices** (`SiteRequest.devices` accepts these directly): `CHPDevice`, `BatteryDevice`, `HeatAccumulatorDevice`, `HeatDemandDevice`, plus the four market-interface variants
- **Convenience helpers**: `four_hour_block_starts`, `build_uniform_acceptance`, `build_zero_activation_revenue`, `LogNormalParams.from_mean_cv`

Field-name parity with the on-prem server's wire schema is pinned by `tests/test_reservation_bid_models.py` and `tests/test_device_property_models.py`. When the server schema changes, these tests fail before any client-side code drifts. See `docs/MIRRORING.md` for the sync procedure.

### Other on-prem methods

Besides reservation bids, `OnPremClient` also exposes the older device-planning + run-inspection surface (still `dict`-typed):

```python
with OnPremClient(base_url=..., api_key=...) as client:
    info = client.health()                                    # /v1/health
    result = client.device_planning(request_payload)          # /v1/device-planning
    run = client.get_run(run_id)                              # /v1/runs/{id}
    runs = client.list_runs(endpoint="reservation-bids")      # /v1/runs?endpoint=...
    cancelled = client.cancel_active()                        # /v1/runs/active/cancel
```

### Error handling

Typed exception hierarchy mirrors the server's error envelope (`onprem_exceptions.py`):

- `BusyError` -- 503 after `BackoffPolicy` retries exhaust
- `InfeasibleScenarioError` -- 422 `INFEASIBLE`. `exc.details.debug_lp_b64` carries the optimizer's LP file (base64) for offline debugging
- `ValidationError` -- 422 `TRANSLATION_ERROR` (or any other 422)
- `AuthenticationError` -- 401
- `CancelledError` -- 499
- `OnPremTimeoutError` -- client-side httpx timeout
- `ServerError` -- unexpected 5xx
- `OnPremError` -- base class for all of the above

## Capabilities

| Feature | Value |
|---------|-------|
| Reservation-bid planner | aFRR+, aFRR-, mFRR+, mFRR- (v1 supports a single ANS-capable device) |
| Device planning | All material types: electricity, heat, gas |
| Resolution | 15-minute or 1-hour |
| Max horizon | Reservation-bid planner: one calendar day. Device planning: longer horizons supported |
| Binary variables | Yes (CHP on/off, max-starts-per-day) |
| ANS abilities | Per-device prequalification declared via `ANSAbility` |
| Idempotency | 24h replay window via `Idempotency-Key` header |

## Supported device types (on-prem)

`Battery`, `CHP`, `HeatAccumulator`, `HeatDemand`, plus the market interfaces `ElectricityImport`, `ElectricityExport`, `GasImport`, `HeatExport`. The `photovoltaic` and `electricity_demand` types are rejected by the on-prem server (`translate_device` raises a `TranslationError`); they're available in the SaaS client.

## MCP server (LLM-driven scenario building)

The package ships an optional [Model Context Protocol](https://modelcontextprotocol.io/) server that exposes **20 tools** for building and submitting operational scenarios from an LLM (Claude Desktop, ChatGPT, Cursor, ...). Wraps `OnPremClient` and runs locally on the user's machine.

### Install

```bash
pip install "site-calc-operational[mcp] @ git+https://github.com/stranma/site-calc-operational.git"
```

The extra pulls `fastmcp>=2.0`. Exposes a console script (`site-calc-operational-mcp`) and a module entry point (`python -m site_calc_operational.mcp`).

### Configure the MCP client

```bash
export SITE_CALC_OPERATIONAL_API_URL="https://operational.algoenergy.cz"
export SITE_CALC_OPERATIONAL_API_KEY="op_..."                    # mint with: site-calc-op create-user
export SITE_CALC_OPERATIONAL_DATA_DIR="$HOME/.site-calc/data"    # optional; for save_data_file output
```

Then register with your MCP client. For Claude Desktop (`%APPDATA%\Claude\claude_desktop_config.json` on Windows, `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "site-calc-operational": {
      "command": "site-calc-operational-mcp",
      "env": {
        "SITE_CALC_OPERATIONAL_API_URL": "https://operational.algoenergy.cz",
        "SITE_CALC_OPERATIONAL_API_KEY": "op_..."
      }
    }
  }
}
```

### Tools exposed (20 total)

| Category | Tools |
|----------|-------|
| Server info | `health`, `get_version` |
| Scenario assembly | `create_scenario`, `add_device`, `remove_device`, `set_timespan`, `set_optimization_config`, `review_scenario`, `delete_scenario`, `list_scenarios` |
| Solving | `solve` (device planning), `cancel_active` |
| **Reservation bids** | **`build_reservation_bids`, `evaluate_reservation_bids`, `most_probable_realization`** |
| Run inspection | `get_run`, `list_runs` |
| Schema / data | `get_device_schema`, `save_data_file`, `fetch_url` |

## SaaS client (legacy)

For backwards compatibility, `OperationalClient` targets the older async SaaS API. New work should use `OnPremClient`.

```python
from site_calc_operational import OperationalClient
# Refer to existing operator runbooks; the API hasn't changed in this release.
```

## Schema mirroring

`site_calc_operational.models` hand-mirrors selected wire schemas from `server-onprem` and device classes from `site-calc-core`. When either side changes upstream, the drift tests in this package fail before any client code silently desyncs. The full directive lives in [`docs/MIRRORING.md`](docs/MIRRORING.md) -- read it before editing anything under `site_calc_operational/models/`.

## Requirements

- Python >= 3.10
- API key with `op_` prefix (operational client)

## Development

```bash
pip install -e ".[dev]"
pytest
ruff format .
mypy site_calc_operational
```

## License

MIT License

## Support

- Issues: https://github.com/site-calc/operational-client/issues
- Schema sync procedure: [`docs/MIRRORING.md`](docs/MIRRORING.md)
- Submodule-local Claude instructions: [`CLAUDE.md`](CLAUDE.md)
