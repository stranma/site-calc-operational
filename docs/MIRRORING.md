# Schema Mirroring Directive

How to keep `site_calc_operational.models` in sync with its upstream sources.
**Read this before editing anything in `site_calc_operational/models/` or in
the server-side files listed below.**

## Why mirrors exist

The client is a public package; the server is private. The client cannot
import `site_calc_onprem` (server) or `site_calc` (core) directly without
dragging a heavy LP/MIP dependency tree into every consumer's environment
and (for the on-prem server) exposing private code. So instead, the client
**hand-mirrors** the request/response wire shapes and the device property
sets as standalone Pydantic models.

Pure additive: existing client methods still accept and return
`dict[str, Any]`. The mirrors are an optional typed layer on top, not a
replacement.

## What's mirrored, from where

| Client mirror | Upstream source of truth | Kind |
|---|---|---|
| `models/reservation_bids.py::ServiceCode` | `site_calc.domain.ans.AncillaryService.code` | Literal of wire codes |
| `models/reservation_bids.py::TimeSpanRequest` | `server-onprem/src/site_calc_onprem/schemas.py::TimeSpanRequest` | Pydantic |
| `models/reservation_bids.py::DeviceRequest` | `server-onprem schemas.py::DeviceRequest` | Pydantic |
| `models/reservation_bids.py::SiteRequest` | `server-onprem schemas.py::SiteRequest` | Pydantic |
| `models/reservation_bids.py::OptimizationConfig` | `server-onprem schemas.py::OptimizationConfig` | Pydantic |
| `models/reservation_bids.py::LogNormalParams` | `server-onprem schemas.py::LogNormalParams` | Pydantic |
| `models/reservation_bids.py::LogNormalFromQuantilesParams` | `server-onprem schemas.py::LogNormalFromQuantilesParams` | Pydantic |
| `models/reservation_bids.py::EmpiricalPercentilesParams` | `server-onprem schemas.py::EmpiricalPercentilesParams` | Pydantic |
| `models/reservation_bids.py::AcceptanceDistributionInput` | `server-onprem schemas.py::AcceptanceDistributionInput` | Tagged union |
| `models/reservation_bids.py::BidAcceptanceEntry` | `server-onprem schemas.py::BidAcceptanceEntry` | Pydantic |
| `models/reservation_bids.py::ReservationBidIn` | `server-onprem schemas.py::ReservationBidIn` | Pydantic |
| `models/reservation_bids.py::ActivationRevenueEntry` | `server-onprem schemas.py::ActivationRevenueEntry` | Pydantic |
| `models/reservation_bids.py::ReservationBidPlanRequest` | `server-onprem schemas.py::ReservationBidPlanRequest` | Pydantic |
| `models/reservation_bids.py::ReservationBidEvaluateRequest` | `server-onprem schemas.py::ReservationBidEvaluateRequest` | Pydantic |
| `models/reservation_bids.py::ReservationBidMPRRequest` | `server-onprem schemas.py::ReservationBidMPRRequest` | Pydantic |
| `models/reservation_bids.py::ReservationBidOut` | `server-onprem translation.py::serialize_reservation_bid` output shape | Hand-spec'd response |
| `models/reservation_bids.py::EvaluationResult` | `server-onprem routers/reservation_bids.py::_run_evaluate` return shape | Hand-spec'd response |
| `models/reservation_bids.py::MostProbableRealizationResult` | `server-onprem translation.py::serialize_most_probable_realization` output shape | Hand-spec'd response |
| `models/reservation_bids.py::ReservationBidPlanResult` | `server-onprem routers/reservation_bids.py::_run_planner` return shape | Hand-spec'd response |
| `models/devices.py::ANSAbility` | `site_calc.domain.ans.ANSAbility` | Pydantic mirror of dataclass |
| `models/devices.py::BatteryProperties` | What `server-onprem translation.py::translate_device("battery")` reads from `properties`, validated against `site_calc.domain.devices.storage.Battery.__init__` | Pydantic |
| `models/devices.py::HeatAccumulatorProperties` | `translate_device("heat_accumulator")` / `HeatAccumulator.__init__` | Pydantic |
| `models/devices.py::CHPProperties` | `translate_device("chp")` / `site_calc.domain.devices.generator.CHP.__init__` | Pydantic |
| `models/devices.py::HeatDemandProperties` | `translate_device("heat_demand")` / `site_calc.domain.devices.consumer.HeatDemand` | Pydantic |
| `models/devices.py::ElectricityImportProperties` | `translate_device("electricity_import")` / `site_calc.domain.devices.market.ElectricityImport` | Pydantic |
| `models/devices.py::ElectricityExportProperties` | `translate_device("electricity_export")` / `ElectricityExport` | Pydantic |
| `models/devices.py::GasImportProperties` | `translate_device("gas_import")` / `GasImport` | Pydantic |
| `models/devices.py::HeatExportProperties` | `translate_device("heat_export")` / `HeatExport` | Pydantic |
| `models/devices.py::*Device` and `TypedDevice` | Compose the matching `*Properties` with the `DeviceRequest` shape; bake in the `type` literal | Pydantic |

Two upstream repos, three sources within them:

- **`server-onprem/src/site_calc_onprem/schemas.py`** — request/response
  Pydantic models the server accepts on the wire.
- **`server-onprem/src/site_calc_onprem/translation.py::translate_device`** —
  per-device-type translator that reads `properties` and constructs the
  matching domain object. Authority on the wire field set per device type.
  Note: temporal-constraint properties (`must_run`, `must_be_idle`,
  `min_continuous_run_hours` on CHP) live as plain entries in
  `properties`, *not* under the (currently-unused) top-level `schedule`
  field on `DeviceRequest`.
- **`site_calc/src/site_calc/domain/devices/*.py` + `domain/ans/base.py`** —
  the underlying domain classes. Authority on the field types and the
  validation ranges (positive scalars, [0,1] fractions, rate windows).

## Drift detection

Two tests pin field-name parity per-class. Either fails when an upstream
field is added, removed, or renamed:

- **`tests/test_reservation_bid_models.py::test_server_wire_field_parity`** —
  asserts each request/leaf model's `model_fields.keys()` matches a literal
  set named `_EXPECTED_FIELDS`, anchored to `server-onprem/schemas.py @ v0.2.0`.
- **`tests/test_device_property_models.py::test_property_field_parity_with_server_translator`** —
  asserts each `*Properties` model's fields match a literal set named
  `_SERVER_TRANSLATE_DEVICE_PROPS`, anchored to `translate_device`'s reads.

Both tests are local-only — they don't import server code. The constants are
the contract; CI fails the moment an unintended rename happens.

## Procedure: server-side field change

When you add, rename, or remove a field in the upstream sources:

1. **Make the server change** (in the parent monorepo's `server-onprem` or
   `site_calc`). Ship it as its own PR with its own CHANGELOG / DECISIONS
   entry on that side. Land that PR first.
2. **Pull the client submodule.** From the parent repo:
   ```
   git -C client-operational fetch origin
   git -C client-operational checkout master
   git -C client-operational pull
   ```
3. **Run the client suite** to see drift tests fail:
   ```
   cd client-operational && uv run pytest tests/test_reservation_bid_models.py tests/test_device_property_models.py
   ```
   Expect a failure naming the class whose fields drifted; the error
   prints `missing` / `extra` field sets.
4. **Update the client mirror** in `models/reservation_bids.py` or
   `models/devices.py` to match the new wire shape. Match field name,
   type, and validation constraints (positive, range, optional vs required).
5. **Update the drift constant** (`_EXPECTED_FIELDS` or
   `_SERVER_TRANSLATE_DEVICE_PROPS`) in the corresponding test.
6. **Re-run the full suite** (`uv run pytest`) — drift test should pass,
   nothing else should regress.
7. **Version bump in `client-operational`:**
   - Patch (`0.2.1 -> 0.2.2`) for a backwards-compatible additive change
     (server added an optional field).
   - Minor (`0.2.x -> 0.3.0`) for a breaking change (server removed a
     field, renamed a field, made an optional field required).
8. **CHANGELOG entry** in `client-operational/CHANGELOG.md` describing
   exactly which mirror changed and why.
9. **Open a client PR.** Title: `feat(models): mirror server-side …` or
   `fix(models): catch up with server …`. Land it.
10. **Bump the submodule pointer** in the parent monorepo.

The parent and the client are versioned independently. The client never
breaks first — server side changes land, then the client catches up.

## Procedure: client-side mirror gap

If you discover a field the server has that the client doesn't mirror yet
(e.g. an existing endpoint with an unmodelled optional knob, or a new
endpoint that has no client wrapper):

1. **Decide the scope.** Adding a single field to an existing mirror is a
   patch bump. Mirroring a whole new endpoint family is a minor bump. Use
   `/design` if it's bigger than a one-file edit.
2. **Mirror the type** following the same patterns: hand-write the Pydantic
   model, add it to `models/__init__.py`'s exports, append it to the table
   at the top of this file.
3. **Pin the field set.** Append the new class to `_EXPECTED_FIELDS` (for
   request/leaf shapes) or `_SERVER_TRANSLATE_DEVICE_PROPS` (for device
   properties). This is what makes the test fail next time the server
   changes the field unannounced.
4. **Add round-trip coverage**: build the typed object, dump it, send it
   through a `respx`-mocked `OnPremClient` call, parse the response back.
   Pattern is in `test_reservation_bid_models.py::test_planner_round_trip_through_client`.

## Procedure: client-only renames / additions

Anything that doesn't change the wire shape (renaming a typed wrapper,
adding a convenience classmethod, expanding a docstring, etc.) is local to
the client and doesn't need parent coordination. Standard patch bump and PR.

## What is **not** mirrored, deliberately

- **Response bodies of `device_planning`, `runs`, `health`,
  `optimal_bidding`.** Still flow as `dict[str, Any]`. `HealthInfo`
  (a dataclass in `api/onprem_client.py`) is the only typed view.
- **`Photovoltaic` and `ElectricityDemand` devices.** The on-prem server
  rejects both in `translate_device` — `TypedDevice` accordingly fails at
  parse if asked to construct them.
- **`diagnostics` block of `ReservationBidPlanResult`.** Kept as
  `dict[str, Any]` because the key set evolves with the planner (variant
  counts, totals_by_bid_count, etc.) and modelling it would force churn on
  every planner perf change.
- **Domain types that don't appear on the wire** — `FlowPort`, `Material`,
  `OptimizedExpr`, `Diagram`, etc. Server-side only.

## Cross-references

- Server-side wire schema: `server-onprem/docs/SPEC.md`, especially
  section 3 (HTTP API) and section 3.9 (reservation-bid endpoints).
- Server-side translation: `server-onprem/src/site_calc_onprem/translation.py`.
- Domain types: `site_calc/src/site_calc/domain/` (private; reference only).
- This file: `client-operational/docs/MIRRORING.md`.
- Drift constants:
  `client-operational/tests/test_reservation_bid_models.py::_EXPECTED_FIELDS`,
  `client-operational/tests/test_device_property_models.py::_SERVER_TRANSLATE_DEVICE_PROPS`.

## When in doubt

The wire is the contract. If a server change doesn't alter what bytes go
over HTTP, no client-side mirror change is needed. If you're changing the
wire and you're not sure whether it touches a mirror, grep `client-operational`
for the field name -- if it shows up in `models/`, you have client work to do.
