# Changelog

All notable changes to `site-calc-operational` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
