# Operational Client Specification

**Package:** `site-calc-operational`
**Version:** 1.0.0
**Purpose:** Day-ahead bidding and short-term dispatch optimization with ancillary services

---

## 1. Overview

The operational client provides Python bindings for the Site-Calc optimization API focused on **short-term operations**:

- Day-ahead electricity market bidding
- Ancillary services optimization (aFRR, mFRR)
- Short-term device scheduling (1-3 days)
- Real-time operational constraints

### 1.1 Key Capabilities

| Feature | Value |
|---------|-------|
| **Max Horizon** | 296 intervals (~3 days) |
| **Resolution** | 15-minute or 1-hour |
| **ANS Optimization** | ✅ Yes (aFRR±, mFRR±) |
| **Binary Variables** | ✅ Yes (CHP on/off) |
| **Timeout** | 300 seconds (5 min) |
| **Endpoints** | `/optimal-bidding`, `/device-planning` |

### 1.2 Use Cases

1. **Optimal Bidding** - Generate multi-step bid curves for day-ahead spot market and ancillary services
2. **Device Planning** - Create operational schedules with locked ancillary service reservations

---

## 2. Installation

```bash
pip install site-calc-operational
```

### 2.1 Dependencies

- Python ≥ 3.10
- pydantic ≥ 2.0
- httpx ≥ 0.24
- python-dateutil ≥ 2.8

---

## 3. Authentication

Operational client requires API key with `op_` prefix:

```python
from site_calc_operational import OperationalClient

client = OperationalClient(
    base_url="https://api.site-calc.example.com",
    api_key="op_1234567890abcdef"  # Must start with 'op_'
)
```

---

## 4. Core Models

### 4.1 TimeSpan

Time period for optimization with interval count:

```python
from datetime import datetime, date
from zoneinfo import ZoneInfo
from site_calc_operational.models import TimeSpan, Resolution

# Full day at 15-minute resolution
ts = TimeSpan.for_day(
    date=date(2025, 11, 6),
    resolution=Resolution.MINUTES_15
)
# intervals=96, start=2025-11-06 00:00:00+01:00

# Custom duration
ts = TimeSpan(
    start=datetime(2025, 11, 6, tzinfo=ZoneInfo("Europe/Prague")),
    intervals=96,
    resolution=Resolution.MINUTES_15
)

# Access computed properties
print(ts.end)        # 2025-11-07 00:00:00+01:00
print(ts.duration)   # timedelta(days=1)
```

**Validation:**
- `start` must use `Europe/Prague` timezone
- `intervals` ≤ 296 for operational clients
- Both `15min` and `1h` resolutions supported

### 4.2 Device Models

All device models support optional `ancillary_services` field:

#### 4.2.1 Battery

```python
from site_calc_operational.models import Battery, AncillaryServices

battery = Battery(
    name="Battery1",
    properties={
        "capacity": 10.0,          # MWh
        "max_power": 5.0,          # MW
        "efficiency": 0.90,        # 0-1
        "initial_soc": 0.5         # 0-1
    },
    ancillary_services={
        "afrr_plus": {
            "can_provide": [1, 1, 1, 1, 1, 1],  # 6 x 4-hour blocks
            "expected_activation_profit": [80.0, 85.0, 88.0, 90.0, 87.0, 82.0]
        },
        "afrr_minus": {
            "can_provide": [1, 1, 1, 1, 1, 1],
            "expected_activation_profit": [75.0, 80.0, 83.0, 85.0, 82.0, 78.0]
        }
    }
)
```

#### 4.2.2 CHP (Combined Heat and Power)

```python
from site_calc_operational.models import CHP, Schedule

chp = CHP(
    name="CHP1",
    properties={
        "gas_input": 8.0,      # MW gas consumption
        "el_output": 3.0,      # MW electricity output
        "heat_output": 4.0,    # MW heat output
        "is_binary": True      # On/off only (not modulating)
    },
    schedule=Schedule(
        min_continuous_run_hours=2.0,
        max_hours_per_day=18.0,
        max_starts_per_day=3,
        can_run=[0]*24 + [1]*48 + [0]*24  # Run only 6am-6pm (96 intervals)
    ),
    ancillary_services={
        "mfrr_plus": {
            "can_provide": [0, 0, 1, 1, 1, 0],  # Daytime blocks only
            "expected_activation_profit": [0, 0, 58.0, 62.0, 60.0, 0]
        }
    }
)
```

#### 4.2.3 Market Devices

```python
from site_calc_operational.models import ElectricityImport, ElectricityExport, GasImport

# Grid import with hourly prices
grid_import = ElectricityImport(
    name="GridImport",
    properties={
        "price": [30.0, 28.0, ...],  # 96 values (EUR/MWh)
        "max_import": 8.0             # MW
    }
)

# Grid export
grid_export = ElectricityExport(
    name="GridExport",
    properties={
        "price": [30.0, 28.0, ...],  # 96 values (EUR/MWh)
        "max_export": 5.0             # MW
    }
)

# Gas supply
gas_import = GasImport(
    name="GasSupply",
    properties={
        "price": [25.0] * 96,  # Constant price
        "max_import": 10.0      # MW
    }
)
```

### 4.3 Site Model

```python
from site_calc_operational.models import Site

site = Site(
    site_id="industrial_site_1",
    description="Industrial facility with CHP and battery",
    devices=[
        battery,
        chp,
        heat_accumulator,
        grid_import,
        grid_export,
        gas_import
    ]
)
```

### 4.4 Market Forecasts

```python
from site_calc_operational.models import MarketForecasts

forecasts = MarketForecasts(
    ancillary_services={
        "afrr_plus": {
            "max_accepted_price_forecast": [15.0, 18.0, 22.0, 25.0, 23.0, 16.0],
            "period_duration_hours": 4
        },
        "afrr_minus": {
            "max_accepted_price_forecast": [12.0, 15.0, 18.0, 20.0, 19.0, 13.0],
            "period_duration_hours": 4
        }
    }
)
```

---

## 5. API Methods

### 5.1 Optimal Bidding

Generate bid curves for DA market and ancillary services:

```python
from site_calc_operational import OperationalClient
from site_calc_operational.models import OptimalBiddingRequest, OptimizationConfig

client = OperationalClient(base_url="...", api_key="op_...")

request = OptimalBiddingRequest(
    sites=[site],
    timespan=timespan,
    market_forecasts=forecasts,
    opportunity_costs={"global": 5.0},
    optimization_config=OptimizationConfig(
        max_bid_steps=10,
        objective="expected_profit",
        time_limit_seconds=300
    )
)

# Submit job
job = client.create_optimal_bidding_job(request)
print(f"Job ID: {job.job_id}, Status: {job.status}")

# Poll for completion
result = client.wait_for_completion(job.job_id, poll_interval=5, timeout=600)

# Access results
for period in result.da_bids:
    print(f"{period.period_start}: {len(period.bids)} bid steps")

for service, bids in result.ancillary_bids.items():
    print(f"{service}: {len(bids)} blocks with bids")
```

### 5.2 Device Planning

Create operational schedule with locked ANS reservations:

```python
from site_calc_operational.models import DevicePlanningRequest, LockedReservations

request = DevicePlanningRequest(
    sites=[site],
    timespan=timespan,
    locked_reservations=LockedReservations(
        afrr_plus={
            "capacity": [0, 0, 3.0, 2.5, 0, 0],  # 6 x 4-hour blocks
            "devices": ["Battery1"]
        }
    ),
    optimization_config=OptimizationConfig(
        objective="maximize_da_revenue",
        time_limit_seconds=300
    )
)

job = client.create_device_planning_job(request)
result = client.wait_for_completion(job.job_id)

# Access device schedules
for site_id, site_result in result.sites.items():
    for device_name, schedule in site_result.device_schedules.items():
        print(f"{device_name}: {schedule.flows}")
        if hasattr(schedule, 'soc'):
            print(f"  SOC: {schedule.soc}")
```

### 5.3 Job Management

```python
# Check status
job = client.get_job_status(job_id)
print(f"Status: {job.status}, Progress: {job.progress}%")

# Get result (when completed)
result = client.get_job_result(job_id)

# Cancel job
cancelled_job = client.cancel_job(job_id)
```

---

## 6. Response Models

### 6.1 Bid Structures

```python
# Day-ahead bid for single period
{
    "period_start": "2025-11-06T00:00:00+01:00",
    "period_end": "2025-11-06T00:15:00+01:00",
    "bids": [
        {"price": 25.0, "quantity_mw": 2.0},   # Positive = export
        {"price": 30.0, "quantity_mw": 4.0},
        {"price": 35.0, "quantity_mw": 5.0}
    ]
}

# Ancillary service bid for 4-hour block
{
    "period_start": "2025-11-06T00:00:00+01:00",
    "period_end": "2025-11-06T04:00:00+01:00",
    "bids": [
        {"price": 15.0, "capacity_mw": 2.0},
        {"price": 18.0, "capacity_mw": 3.0},
        {"price": 22.0, "capacity_mw": 4.0}
    ]
}
```

### 6.2 Device Schedule

```python
{
    "Battery1": {
        "flows": {
            "electricity": [2.0, -3.5, 0.0, ...]  # 96 values (MW)
        },
        "soc": [0.5, 0.48, 0.51, ...],            # 96 values (0-1)
        "ancillary_reservations": {
            "afrr_plus": [0, 0, 2.0, 2.0, ...],   # 96 values (MW)
            "afrr_minus": [1.5, 1.5, 0, 0, ...]
        }
    },
    "CHP1": {
        "flows": {
            "gas": [-8.0, -8.0, 0.0, ...],        # 96 values (MW, negative = consumption)
            "electricity": [3.0, 3.0, 0.0, ...],  # 96 values (MW, positive = generation)
            "heat": [4.0, 4.0, 0.0, ...]
        },
        "binary_status": [1, 1, 0, ...],          # 96 values (0/1)
        "ancillary_reservations": {
            "mfrr_plus": [0, 0, 1.5, 1.5, ...]
        }
    }
}
```

---

## 7. Error Handling

```python
from site_calc_operational.exceptions import (
    ApiError,
    ValidationError,
    AuthenticationError,
    TimeoutError,
    OptimizationError
)

try:
    result = client.create_optimal_bidding_job(request)
except ValidationError as e:
    print(f"Invalid request: {e.details}")
except AuthenticationError as e:
    print(f"Auth failed: {e.message}")
except OptimizationError as e:
    print(f"Solver error: {e.code} - {e.message}")
    if e.details:
        print(f"Details: {e.details}")
except TimeoutError as e:
    print(f"Request timeout after {e.timeout} seconds")
```

---

## 8. Complete Example

```python
from datetime import date
from zoneinfo import ZoneInfo
from site_calc_operational import OperationalClient
from site_calc_operational.models import (
    TimeSpan, Resolution, Site, Battery, CHP, ElectricityImport,
    OptimalBiddingRequest, MarketForecasts, OptimizationConfig
)

# Initialize client
client = OperationalClient(
    base_url="https://api.site-calc.example.com",
    api_key="op_1234567890abcdef"
)

# Create timespan (1 day, 15-min resolution)
timespan = TimeSpan.for_day(date(2025, 11, 6), Resolution.MINUTES_15)

# Define devices
battery = Battery(
    name="Battery1",
    properties={"capacity": 10.0, "max_power": 5.0, "efficiency": 0.90, "initial_soc": 0.5},
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

site = Site(site_id="test_site", devices=[battery, grid_import])

# Market forecasts
forecasts = MarketForecasts(
    ancillary_services={
        "afrr_plus": {
            "max_accepted_price_forecast": [15.0, 18.0, 22.0, 25.0, 23.0, 16.0],
            "period_duration_hours": 4
        }
    }
)

# Create request
request = OptimalBiddingRequest(
    sites=[site],
    timespan=timespan,
    market_forecasts=forecasts,
    opportunity_costs={"global": 5.0},
    optimization_config=OptimizationConfig(max_bid_steps=10)
)

# Submit and wait
job = client.create_optimal_bidding_job(request)
result = client.wait_for_completion(job.job_id, timeout=600)

# Display results
print(f"Total Profit: €{result.summary.expected_profit:,.2f}")
print(f"DA Revenue: €{result.summary.total_da_revenue:,.2f}")
print(f"ANS Revenue: €{result.summary.total_ancillary_revenue:,.2f}")
```

---

## 9. Validation Rules

### 9.1 TimeSpan Validation

- Maximum 296 intervals
- Both 15-minute and 1-hour resolution supported
- Timezone must be `Europe/Prague`

### 9.2 Array Length Validation

| Array Type | 15-min | 1-hour |
|------------|--------|--------|
| Schedule arrays (can_run, must_run) | 96 | 24 |
| Price profiles | 96 | 24 |
| Demand profiles | 96 | 24 |
| ANS capability (can_provide) | 6 (4-hour blocks) | 6 |

### 9.3 ANS Service Types

- `afrr_plus` - Automatic Frequency Restoration Reserve (upward)
- `afrr_minus` - Automatic Frequency Restoration Reserve (downward)
- `mfrr_plus` - Manual Frequency Restoration Reserve (upward)
- `mfrr_minus` - Manual Frequency Restoration Reserve (downward)

Each service has:
- `can_provide`: 6 binary values (one per 4-hour block)
- `expected_activation_profit`: 6 float values (EUR/MW/h)

---

## 10. Limits and Constraints

| Limit | Value |
|-------|-------|
| Max intervals | 296 |
| Max sites | 100 |
| Max devices per site | 50 |
| Max bid steps | 20 |
| Request timeout | 300 seconds |
| Request size | 10 MB |

---

## 11. Migration from Legacy

If migrating from legacy client:

**Before:**
```python
from site_calc.client import OptimizationClient
client = OptimizationClient(api_key="...")
```

**After:**
```python
from site_calc_operational import OperationalClient
client = OperationalClient(api_key="op_...")  # Note: op_ prefix required
```

**Device property changes:**
- `capacity_mwh` → `capacity`
- `power_mw` → `max_power`
- No more `grid_connection` object (use market devices)

---

## 12. Support

- **Documentation**: https://docs.site-calc.example.com/operational-client
- **Issues**: https://github.com/site-calc/operational-client/issues
- **Examples**: https://github.com/site-calc/operational-client/tree/main/examples
