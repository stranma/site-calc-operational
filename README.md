# site-calc-operational

Python client for the site-calc operational planning server. You bring the
day-ahead price forecast and the reservation-market acceptance forecast; the
server returns the bids for one delivery day of a battery (or CHP) site.

Two calls per day, in the order the markets close:

1. `plan_reservation()` before the reservation gate: capacity bids per
   4-hour block for the ancillary services you are prequalified for, with the
   expected revenue and the most likely outcome.
2. `plan_day_ahead()` after the reservation results are known and before the
   day-ahead gate: signed price-taker bids per quarter-hour that honour the
   cleared reservations, plus the planned schedule and the end-of-day state
   of charge to carry into tomorrow.

The client is synchronous, fully typed (`py.typed`), and depends only on
`httpx` and `pydantic`. Version 0.2.x talks to a 0.2.x server.

## Installation

```bash
pip install site-calc-operational        # once published; until then:
pip install git+https://github.com/stranma/site-calc-operational.git
```

Python 3.10 or newer.

## Quick start

```python
from datetime import date
from site_calc_operational import (
    ANSAbility, AnsForecastEntry, Battery, ClearedReservation, Day, ElectricityExport,
    ElectricityImport, LogNormal, OperationalClient, PlanDayAheadRequest, PlanReservationRequest, Site,
)

site = Site(
    site_id="my-bess",
    devices=[
        Battery(
            name="BESS", capacity_mwh=2.0, max_power_mw=1.0, efficiency=0.9,
            initial_soc_mwh=1.0,                      # state at midnight; tomorrow: plan.soc_end_mwh
            ans_abilities=[
                ANSAbility(service="afrr_plus", min_device_power_rate=0.0, max_device_power_rate=1.0),
                ANSAbility(service="afrr_minus", min_device_power_rate=0.0, max_device_power_rate=1.0),
            ],
        ),
        ElectricityImport(name="GridBuy", max_import_mw=1.0, buy_fee_eur_per_mwh=1.5),
        ElectricityExport(name="GridSell", max_export_mw=1.0, sell_fee_eur_per_mwh=1.0),
    ],
)

day = Day(
    date=date(2026, 4, 15), tz="Europe/Prague",
    da_price_eur_per_mwh_d=prices_d,      # 96 quarter-hour prices for the delivery day
    da_price_eur_per_mwh_d1=prices_d1,    # 96 for the day after: required for a battery
)

forecast = [
    AnsForecastEntry(service=s, block_index=b, distribution=LogNormal(mu=2.0, sigma=0.5),
                     expected_activation_eur_per_mw_h=1.0)
    for s in ("afrr_plus", "afrr_minus") for b in range(6)      # one entry per service and 4-hour block
]

with OperationalClient("https://operational.example.com", "op_...") as client:
    # 1. before the reservation gate
    plan = client.plan_reservation(
        PlanReservationRequest(site=site, day=day, services=["afrr_plus", "afrr_minus"], ans_forecast=forecast),
        idempotency_key="my-bess-2026-04-15-reservation",     # a retry replays instead of re-planning
    )
    for bid in plan.bids:
        print(bid.service, bid.block_index, bid.volume_mw, bid.capacity_price_eur_per_mw_h)
    print(plan.expected_revenue.total)

    # 2. after clearing, before the day-ahead gate
    cleared = [ClearedReservation(service="afrr_plus", block_index=2, volume_mw=1.0)]   # what actually cleared
    da = client.plan_day_ahead(
        PlanDayAheadRequest(site=site, day=day, cleared_reservations=cleared),
        idempotency_key="my-bess-2026-04-15-day-ahead",
    )
    for bid in da.bids:              # 96, signed: > 0 sells, < 0 buys
        ...
    tomorrow_initial_soc = da.soc_end_mwh    # or your measured state of charge at midnight
```

`examples/plan_bess_day.py` is the runnable version.

## What you send

- **`Site`**: one device per type. A battery site is `Battery` +
  `ElectricityExport` (+ `ElectricityImport` to charge from the grid). A CHP
  site is `CHP` + `GasImport` + `ElectricityExport` (+ `HeatExport`). Only
  one device carries `ans_abilities`.
- **`Day`**: the delivery day, its timezone, and 96 day-ahead prices for it.
  A battery also needs the 96 prices of the following day: the planner
  values the energy left in the battery at midnight against them. Days on
  which the clocks change cannot be planned.
- **`ans_forecast`**: for each requested service and each of the six 4-hour
  blocks, how likely a bid is to clear as a function of its price
  (`LogNormal`, `LogNormalFromQuantiles` or `EmpiricalPercentiles`), and
  the activation revenue you expect on top of the capacity payment.
- **`ReservationParams`**: `planner="sitecalc"` (default) co-optimises
  reservation and day-ahead and takes around ten minutes for two services;
  `planner="baseline"` answers in seconds.

Request models reject unknown fields and check the structural rules
locally, before any HTTP call: one device per type, an export leg, D+1
prices for a battery, services within the device's `ans_abilities`,
forecast coverage, SOC within capacity, well-formed distributions. These
raise `pydantic.ValidationError` when you build the request. What only
the server can judge (timezone names, DST days, reservations against the
power range, feasibility) comes back as the package's own exceptions
below.

## What you get back

- **`ReservationPlan`**: `bids` (service, block, MW, EUR/MW/h, clearing
  probability), `expected_revenue` (reservation, activation, day-ahead,
  total), `most_probable_realization`, `diagnostics`, and `run` (server and
  engine versions, planner, horizon, solve time).
- **`DayAheadPlan`**: 96 `bids`, the `schedule` over the whole horizon (net
  flow, state of charge and the bands the reservations impose),
  `soc_end_mwh`, `objective_eur`, `market_fees_eur`, `run`.

`docs/WIRE.md` lists every field.

## Errors and retries

Every server-side failure is an `OperationalError` subclass with `code`,
`message`, `details` and `http_status`: `ValidationError` (the server
rejected the request; not the `pydantic.ValidationError` you get while
building one), `DayNotPlannableError` (DST day, or a battery day without D+1
prices), `InfeasibleError`, `AuthenticationError`, `IdempotencyConflictError`,
`BusyError`, `ServerError`, `OperationalTimeoutError`, `TransportError`.

The server computes one plan at a time. On `503 BUSY` the client waits and
retries (default: up to 5 times, 30 s doubling to 120 s, honouring
`Retry-After`); pass `busy_retry=None` to raise at once, or your own
`BusyRetry`. The default read timeout is 20 minutes; always send an
`idempotency_key` on planning calls so that a retry after a dropped
connection returns the stored plan (`client.last_call_was_replay`).

## Runs

Every call is stored on the server. `list_runs()` pages through yours (newest
first), `get_run(id)` returns one with the request as validated and the
response as returned, `cancel_active()` interrupts the plan in progress.

## Development

```bash
uv venv && uv sync --extra dev
uv run pytest                                 # mocked HTTP, no server needed
uv run pytest -m production                   # live server: SITE_CALC_OPERATIONAL_URL, SITE_CALC_OPERATIONAL_API_KEY
uv run ruff check site_calc_operational tests examples && uv run mypy site_calc_operational
```

## License

MIT.
