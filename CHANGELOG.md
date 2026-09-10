# Changelog

All notable changes to `site-calc-operational` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0] - 2026-09-10

Rewritten from scratch against the operational planning server 0.2. The
version now tracks the server's MAJOR.MINOR: 0.2.x talks to a 0.2.x server.

### Added
- `OperationalClient` with `plan_reservation()` and `plan_day_ahead()`: the
  two calls of a delivery day, both returning typed results
  (`ReservationPlan`, `DayAheadPlan`). `Idempotency-Key` support so a retry
  after a timeout replays instead of re-planning; automatic wait-and-retry
  on `503 BUSY` (`BusyRetry`, honours `Retry-After`).
- `health()`, `get_run()`, `list_runs()`, `cancel_active()`.
- Typed request models: `Site` with `Battery`, `CHP`, `GasImport`,
  `HeatExport`, `ElectricityImport`, `ElectricityExport`; `Day` with the
  delivery day's prices and the D+1 prices a battery needs;
  `AnsForecastEntry` with `LogNormal`, `LogNormalFromQuantiles` or
  `EmpiricalPercentiles` acceptance; `ReservationParams`,
  `ClearedReservation`, `DayAheadParams`. Server rules (one device per type,
  forecast coverage, SOC within capacity) are checked before sending.
- One exception per server error code (`ValidationError`,
  `DayNotPlannableError`, `InfeasibleError`, `BusyError`,
  `IdempotencyConflictError`, ...); timeouts and connection failures raise
  `OperationalTimeoutError` / `TransportError`.
- `py.typed`; `examples/plan_bess_day.py`; `docs/WIRE.md`.

### Removed
- Everything from 0.3.x: `OnPremClient`, the legacy SaaS `OperationalClient`
  job API, the reservation-bid and device-planning models, the MCP server
  and its 20 tools, the schema-mirroring documents. The server they talked
  to no longer exists; nothing was migrated.

## [0.3.1] and earlier

Superseded. See the git history for the previous client's changelog.
