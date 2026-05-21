# Changelog

All notable changes to `site-calc-operational` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.3.0] - 2026-05-21

Builds on the unreleased 0.2.1 typed-models work with API polish from a
dogfood test (an SDK-only integrator wrote a working
`build_reservation_bids` call against a live deployment and reported back
on what was unclear). 0.2.1 was never published; its features are folded
in here.

### Added

- **`SiteRequest.devices` now accepts the typed device wrappers directly.**
  v0.2.1 introduced `CHPDevice`, `BatteryDevice`, ..., but
  `SiteRequest.devices` was typed `list[DeviceRequest]`, so the typed
  wrappers couldn't actually be used at the site level. Now typed as
  `list[Union[TypedDevice, DeviceRequest]]` with discriminator dispatch
  on `type`: a `chp` dict parses straight to `CHPDevice`, an unknown
  `type` falls through to `DeviceRequest` (forward-compatible with
  future server-side device types).
- **`LogNormalParams.from_mean_cv(mean, cv)`** classmethod -- construct
  the log-normal from EUR/MW/h mean + coefficient of variation, avoiding
  the off-by-`sigma**2/2` mistake of `mu = ln(mean)`. The math is
  derivable from the docstring but every caller would write the same
  helper otherwise.
- **`four_hour_block_starts(timespan)`** helper -- returns the six block
  starts (00/04/08/12/16/20 in the timespan's tz) the planner needs.
- **`build_uniform_acceptance(timespan, services, distribution)`** -- one
  shot for "this distribution for every `(service, block)`", which the
  planner requires the full Cartesian product of. Eliminates a common
  source of `TRANSLATION_ERROR` on first use.
- **`build_zero_activation_revenue(timespan, services)`** -- the
  conservative "no activation upside" default.
- **Typed Pydantic models for the reservation-bid endpoints** under
  `site_calc_operational.models`. Hand-mirrored from the on-prem server's
  schemas with field-name parity pinned by `test_reservation_bid_models.py`.
  Public surface:
  - `ReservationBidPlanRequest`, `ReservationBidEvaluateRequest`,
    `ReservationBidMPRRequest`

### Added

- **Typed Pydantic models for the reservation-bid endpoints** under
  `site_calc_operational.models`. Hand-mirrored from the on-prem server's
  schemas with field-name parity pinned by `test_reservation_bid_models.py`.
  Public surface:
  - `ReservationBidPlanRequest`, `ReservationBidEvaluateRequest`,
    `ReservationBidMPRRequest`
  - `ReservationBidPlanResult`, `EvaluationResult`,
    `MostProbableRealizationResult`, `ReservationBidOut`
  - `AcceptanceDistributionInput` (discriminated union: `LogNormalParams`,
    `LogNormalFromQuantilesParams`, `EmpiricalPercentilesParams`)
  - `BidAcceptanceEntry`, `ReservationBidIn`, `ActivationRevenueEntry`
  - Shared structural types: `TimeSpanRequest`, `SiteRequest`,
    `DeviceRequest`, `OptimizationConfig`, `ServiceCode`
- **Typed device-properties + typed-device wrappers** mirroring the
  domain devices in `site_calc.domain.devices.*`. Closes the discovery gap
  in `DeviceRequest.properties` (`dict[str, Any]`): an IDE now shows the
  per-type field set, and `extra='forbid'` catches misspellings at
  construction time. Field-name parity with the on-prem server's
  `translate_device` pinned by `test_device_property_models.py`. Public
  surface:
  - Properties: `BatteryProperties`, `HeatAccumulatorProperties`,
    `CHPProperties`, `HeatDemandProperties`, `ElectricityImportProperties`,
    `ElectricityExportProperties`, `GasImportProperties`,
    `HeatExportProperties`
  - Typed devices (one per type): `BatteryDevice`, `HeatAccumulatorDevice`,
    `CHPDevice`, `HeatDemandDevice`, `ElectricityImportDevice`,
    `ElectricityExportDevice`, `GasImportDevice`, `HeatExportDevice`
  - `TypedDevice` -- tagged union of all of the above, discriminated by
    `type`; lets a caller `TypeAdapter(TypedDevice).validate_python({...})`
    and have the right subclass dispatched
  - `ANSAbility` -- Pydantic mirror of `site_calc.domain.ans.ANSAbility`
    with the same rate-window validation
- The `OnPremClient` methods still accept and return `dict[str, Any]` --
  this release is **additive**, not a breaking change. Typical usage:
  ```python
  chp_props = CHPProperties(
      gas_input=2.5, el_output=1.0, heat_output=1.0, is_binary=True,
      ans_abilities=[ANSAbility(service="afrr_plus",
                                min_device_power_rate=0.0,
                                max_device_power_rate=1.0)],
  )
  req = ReservationBidPlanRequest(
      sites=[SiteRequest(site_id="...", devices=[
          DeviceRequest(name="CHP-bin", type="chp",
                        properties=chp_props.model_dump(mode="json")),
      ])],
      timespan=..., services=[...], acceptance=[...],
  )
  raw = client.build_reservation_bids(req.model_dump(mode="json"))
  result = ReservationBidPlanResult.model_validate(raw)
  ```

### Changed

- **README rewritten around the on-prem reservation-bid flow.** The Quick
  Start now showcases the typed-model + `OnPremClient` workflow that
  v0.3.0 documents end-to-end. The stale SaaS `OperationalClient`
  example (which referenced symbols that don't exist on this branch:
  `wait_for_completion`, `MarketForecasts`, etc.) is replaced by a brief
  legacy section pointing to operator runbooks.
- **MCP tool count documented as 20** (up from 17 in v0.1.0): adds
  `build_reservation_bids`, `evaluate_reservation_bids`,
  `most_probable_realization`.
- **Docstring fills** on the v0.2.1 surface, driven by gaps from the
  dogfood test:
  - `LogNormalParams`: declares the unit (EUR/MW/h), documents the
    mean/median/CV transforms.
  - `ReservationBidPlanRequest`: documents the acceptance
    Cartesian-coverage rule (services × 6 blocks) and the
    `assume_maximal` correctness tradeoff with `winner_is_maximal`.
  - `ActivationRevenueEntry`: clarifies "additional on top of capacity
    payment" semantics.
  - `OnPremClient.*`: 24-hour idempotency TTL stated explicitly.

### Notes

- `device_planning`, `runs`, `health`, and `optimal_bidding` response shapes
  still flow as `dict[str, Any]`. `HealthInfo` (a dataclass in
  `api/onprem_client.py`) remains the only typed view for `/v1/health`.
- `photovoltaic` and `electricity_demand` are not modelled because the
  on-prem server rejects both -- a `TypedDevice` with `type="photovoltaic"`
  fails at parse time rather than at the server.

## [0.2.0] - 2026-05-20

### Added

- **`OnPremClient.build_reservation_bids(request)`** -- wraps the new
  `POST /v1/reservation-bids` endpoint. Server returns the day-ahead
  reservation-bid plan plus its own `most_probable_realization` and a
  re-evaluated `expected_revenue` in one round-trip.
- **`OnPremClient.evaluate_reservation_bids(request)`** -- wraps
  `POST /v1/reservation-bids/evaluate`. Scores a caller-supplied bid set via
  `expected_plan_revenue` (planner with the search removed).
- **`OnPremClient.most_probable_realization(request)`** -- wraps
  `POST /v1/reservation-bids/most-probable-realization`. Returns the modal
  contracts, day-ahead baseline, realized revenue, and joint probability.
- **MCP tools**: `build_reservation_bids`, `evaluate_reservation_bids`,
  `most_probable_realization`. Each takes a `scenario_id` (for site +
  timespan) plus reservation-bid-specific kwargs (`services`, `acceptance`,
  `bids`, etc.). MCP tool count: 17 -> 20.
- The on-prem server returns the optimizer's debug LP inline on `INFEASIBLE`;
  the MCP tool decodes the base64 blob, writes it to
  `SITE_CALC_OPERATIONAL_DATA_DIR`, and rewrites
  `exc.details.debug_lp_path` so the LLM can point a human at the file. The
  same handler that ships for `solve` now runs on the reservation-bid path.

### Notes

- All three new methods accept `idempotency_key` and respect the same
  `BackoffPolicy` 503 retry as `device_planning`.
- 422 `INFEASIBLE` / `TRANSLATION_ERROR`, 503 `BUSY`, 401 dispatch all fall
  out of the existing `from_response` mapping -- no new exception types
  needed.

## [0.1.0] - 2026-05-07

First public release. The package versions independently from the rest of the
site-calc monorepo (which is on the 1.2.x series); operational starts at 0.1.0
to honestly reflect that this is its first published version. The 0.x prefix
also signals that the SDK surface may change before 1.0.

### Added

- **`OnPremClient`** — synchronous HTTP client for self-hosted
  `server-onprem` deployments. Methods: `health`, `device_planning`,
  `optimal_bidding` (raises `NotImplementedOnServer` until the server endpoint
  is implemented), `get_run`, `list_runs`, `cancel_active`. Uses an
  `Idempotency-Key` header for replay-safe retries and `BackoffPolicy` for 503
  retry behaviour.
- **`OperationalClient`** — async client for the SaaS REST API; same surface
  area as previous unreleased iterations (job submission, polling, job
  cancellation, multi-site bidding).
- **Typed exception hierarchy** (`OnPremError`, `BusyError`, `ValidationError`,
  `AuthenticationError`, `CancelledError`, `NotImplementedOnServer`,
  `ClientError`, `ServerError`, `OnPremTimeoutError`, `IdempotencyConflict`).
  `from_response()` maps HTTP status codes to the right subclass, tolerating
  non-JSON 4xx bodies (e.g. plaintext from intermediate proxies).
- **MCP server** under the `[mcp]` extra. Exposes 17 tools that let an LLM
  build operational device-planning scenarios incrementally and submit them
  against an on-prem server. Includes a scenario store, profile resolution
  (scalar / list / file ref), CSV save/load, URL fetch with private-network
  blocklist, and an extensive `instructions=` cheat sheet covering working
  folder, horizon sizing, solver choice, MIP-gap defaults, idempotency keys,
  battery SOC anchoring, CHP MIP/LP tradeoff, market sign conventions, and
  error-class semantics.
- **CI / Publish workflows** (`.github/workflows/ci.yml`,
  `.github/workflows/publish.yml`) using PyPI Trusted Publishing (OIDC).

### Security

- **Path containment**: `save_data_file` and `fetch_url` reject paths that
  resolve outside the configured data root (`SITE_CALC_OPERATIONAL_DATA_DIR`,
  default `~/Documents/site-calc-data`).
- **SSRF defence**: `fetch_url` resolves DNS up-front and rejects loopback,
  RFC 1918 private, link-local (incl. AWS metadata at 169.254.169.254),
  multicast, reserved, and unspecified addresses. Redirect targets are
  re-validated against the same rules.
