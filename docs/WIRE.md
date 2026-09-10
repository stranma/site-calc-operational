# The wire, as the client sees it

Everything the server accepts and returns, in the terms of the models in
`site_calc_operational.models`. The server is the authority; this page is
kept in step with it release by release (same MAJOR.MINOR).

Times are the site's local time (`Day.tz`). Every array indexed by
quarter-hour starts at local midnight of the delivery day D.

## Endpoints

| Method | Path | Client method |
|---|---|---|
| GET | `/v1/health` | `health()` |
| POST | `/v1/plan/reservation` | `plan_reservation(request, idempotency_key=...)` |
| POST | `/v1/plan/day-ahead` | `plan_day_ahead(request, idempotency_key=...)` |
| GET | `/v1/runs` | `list_runs(endpoint=, status=, limit=, before=)` |
| GET | `/v1/runs/{id}` | `get_run(id)` |
| POST | `/v1/runs/active/cancel` | `cancel_active()` |

Auth: `Authorization: Bearer op_...` on everything except health. One plan is
computed at a time; a second caller gets `503 BUSY` with `Retry-After`.

## Site

```
Site {site_id, devices: [Device, ...]}
Battery            {type="battery", name, capacity_mwh, max_power_mw, efficiency, initial_soc_mwh, ans_abilities[]}
CHP                {type="chp", name, gas_input_mw, el_output_mw, heat_output_mw, is_binary=false,
                    max_starts_per_day?, min_continuous_run_hours?, ans_abilities[]}
GasImport          {type="gas_import", name, price_eur_per_mwh, max_import_mw?}
HeatExport         {type="heat_export", name, price_eur_per_mwh, max_export_mw?, max_export_total_mwh?}
ElectricityImport  {type="electricity_import", name, max_import_mw?, buy_fee_eur_per_mwh=0}
ElectricityExport  {type="electricity_export", name, max_export_mw?, sell_fee_eur_per_mwh=0}
ANSAbility         {service, min_device_power_rate, max_device_power_rate, soc_reserve_fraction=0.25}
```

Rules (checked by the client before sending, and by the server): at most one
device per type, unique names, an `ElectricityExport` always, a `GasImport`
with every `CHP`, `ans_abilities` on one device only, `initial_soc_mwh` not
above `capacity_mwh`, requested and cleared services within that device's
`ans_abilities`, `chp_pins` only with a `CHP`, D+1 prices with a battery. Market fees are applied when planning (buy price plus
fee, sell price minus fee); settlement stays at the market price.

Services: `afrr_plus`, `afrr_minus`, `mfrr_plus`, `mfrr_minus`.

## Day

```
Day {date, tz, da_price_eur_per_mwh_d[96], da_price_eur_per_mwh_d1[96]?}
```

For a battery site `da_price_eur_per_mwh_d1` is mandatory: the planner
dispatches over the 192 quarter-hours of D and D+1 so the midnight state of
charge is valued, and commits only day D. A CHP-only site plans the 96
quarter-hours of D. Days on which the clocks change, and for a battery site
the day before one, are refused by the server with `DayNotPlannableError`
(the client always sends 96 prices; it does not know the timezone rules).

## Reservation step

Request:

```
PlanReservationRequest {site, day, services: [ServiceCode, ...],
  ans_forecast: [AnsForecastEntry {service, block_index 0..5, distribution, expected_activation_eur_per_mw_h=0}, ...],
  params: ReservationParams {planner="sitecalc"|"baseline", assume_maximal=false, px_percentile=0.5,
                             terminal_soc_fraction?},
  metadata?}
LogNormal              {type="lognormal", mu, sigma}
LogNormalFromQuantiles {type="lognormal_from_quantiles", quantiles: [[p, price], ...]}
EmpiricalPercentiles   {type="empirical_percentiles", breakpoints: [[price, p_clear], ...]}
```

One forecast entry per (service, block) for every requested service. Block
`b` covers local hours `4b` to `4b + 4`.

Result:

```
ReservationPlan {
  bids: [ReservationBid {service, block_index, interval_start, volume_mw, capacity_price_eur_per_mw_h,
                         forecast_clear_probability}, ...],
  expected_revenue: {reservation, activation, da_arbitrage, total},
  most_probable_realization: {contracts: [ReservationBid without forecast_clear_probability, ...],
                              baseline_da, realized_revenue, joint_probability},
  diagnostics: {...},
  run: RunInfo {site_calc_version, site_calc_commit_sha, planner, params, horizon_qh, solve_seconds}}
```

Runtime: `baseline` seconds; `sitecalc` with one service seconds, with two
services around ten minutes. The client's default timeout is 20 minutes.

## Day-ahead step

Request:

```
PlanDayAheadRequest {site, day,
  cleared_reservations: [ClearedReservation {service, block_index, volume_mw, clearing_price_eur_per_mw_h?}, ...],
  chp_pins: {block_index: mw, ...},
  params: DayAheadParams {terminal_soc_fraction?},
  metadata?}
```

Battery sites pass `cleared_reservations`; CHP sites pass `chp_pins`.

Result:

```
DayAheadPlan {
  bids: [DayAheadBid {qh_index, interval_start, volume_mw (signed), price_eur_per_mwh}, ...]   96 entries
  schedule: Schedule {net_flow_mw[horizon], committed_qh, soc_mwh[]?, soc_min_mwh[]?, soc_max_mwh[]?,
                      band_lo_mw[]?, band_hi_mw[]?},
  soc_end_mwh?, objective_eur, expected_da_value_eur, market_fees_eur, run: RunInfo}
```

Bids are price-taker: `volume_mw > 0` sells at -500 EUR/MWh, `< 0` buys at
+4000 EUR/MWh. `soc_mwh[t]` is the state at the start of interval `t`;
`soc_end_mwh` is the state at midnight after day D and is what you pass as
the next day's `initial_soc_mwh`.

## Health and runs

```
HealthInfo {status "ok"|"degraded", service_version, site_calc_version, site_calc_commit_sha, db_ok, active_solve}
RunSummary {id, endpoint "plan-reservation"|"plan-day-ahead", status "ok"|"error"|"cancelled", created_at,
            finished_at?, duration_ms?, solver_status?, client_idempotency_key?}
RunDetail  = RunSummary + {request, response}     the body as validated and the body as returned
RunsPage   {runs: [RunSummary, ...], next_before?}
```

`list_runs(endpoint=, status=, limit=, before=)` pages newest first; pass a
page's `next_before` as `before` for the next page. `cancel_active()`
returns True when a plan was interrupted and False when the server was idle.

## Errors

| Exception | HTTP | Server code |
|---|---|---|
| `AuthenticationError` | 401 | `UNAUTHENTICATED` |
| `NotFoundError` | 404 | `NOT_FOUND` |
| `IdempotencyConflictError` | 409 | `IDEMPOTENCY_KEY_REUSED` |
| `RequestTooLargeError` | 413 | `REQUEST_TOO_LARGE` |
| `ValidationError` | 422 | `VALIDATION_ERROR`, `TRANSLATION_ERROR` |
| `DayNotPlannableError` (a `ValidationError`) | 422 | `DST_DAY_UNSUPPORTED`, `TWO_DAY_HORIZON_REQUIRED` |
| `InfeasibleError` | 422 | `INFEASIBLE` |
| `UnboundedError` | 422 | `UNBOUNDED` |
| `CancelledError` | 499 | `CANCELLED` |
| `ServerError` | 500, other | `INTERNAL_ERROR`, none |
| `BusyError` | 503 | `BUSY` (after the retry policy is exhausted) |
| `OperationalTimeoutError` | none | the HTTP timeout elapsed |
| `TransportError` | none | connection failed |

Every exception carries `code`, `message`, `details` and `http_status`.

## Idempotency

Send `idempotency_key` on planning calls. The same key with the same body
within 24 hours returns the stored plan (`client.last_call_was_replay` is
True); the same key with a different body is `IdempotencyConflictError`.
