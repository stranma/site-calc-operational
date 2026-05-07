---
title: "site-calc-operational SDK - Sync Companion for On-Prem Server"
date: 2026-04-30
lang: en
footer-left: "Confidential - internal use only"
---

**Status:** Draft
**Owner:** Martin Stransky
**Parent:** `server-onprem/docs/SPEC.md` (defines the wire protocol)

## Why this exists

The new on-prem server (`server-onprem/`) exposes a **synchronous** HTTP API: `POST /v1/device-planning` blocks until the solver finishes and returns the result in the response body. The existing operational client SDK (`OperationalClient` in `site_calc_operational.api.client`) targets the SaaS server, which uses async **submit-then-poll** semantics (`create_*_job` + `wait_for_completion`).

These are different client shapes. Bolting a sync mode onto `OperationalClient` would muddy its contract. We add a **separate sync companion class** so consumers pick the right one for their backend.

## Out of scope

- **Removing or rewriting `OperationalClient`.** The async client stays as-is for SaaS-server users.
- **A unified backend-detecting client.** Two backends, two classes; consumers know which one they target.
- **CLI tooling for end consumers.** Consumers integrate the SDK into their own scripts. The on-prem operator's CLI (`site-calc-op`) lives in the server image, not in this SDK.

## What gets added

A new module `site_calc_operational.api.onprem_client` exposing one class:

```python
from site_calc_operational import OnPremClient
from site_calc_operational.models import DevicePlanningRequest

client = OnPremClient(
    base_url="https://onprem.example.com",
    api_key="op_...",
    timeout_seconds=600,        # client-side cap, server caps at 600 too
    busy_retry=BackoffPolicy(   # 503 handling
        max_retries=3,
        initial_delay_seconds=10,
        max_delay_seconds=60,
    ),
)

result = client.device_planning(request)
# returns DevicePlanningResponse on 200; raises typed exceptions on 401/422/503-exhausted/etc.
```

### Methods

| Method | Wraps | Notes |
|---|---|---|
| `device_planning(request, *, idempotency_key=None) -> DevicePlanningResponse` | `POST /v1/device-planning` | Sync. Auto-retries on 503 per `busy_retry`. |
| `optimal_bidding(request, *, idempotency_key=None) -> ...` | `POST /v1/optimal-bidding` | Until server implements it: raises `NotImplementedOnServer` (parsed from `501` envelope). |
| `get_run(run_id) -> Run` | `GET /v1/runs/{id}` | |
| `list_runs(*, endpoint=None, status=None, limit=50, before=None) -> RunList` | `GET /v1/runs` | Returns paginated cursor `next_before`. |
| `cancel_active() -> CancelResult` | `POST /v1/runs/active/cancel` | Returns whether something was cancelled. |
| `health() -> HealthInfo` | `GET /v1/health` | Unauthenticated. Returns versions and `db_ok`. |

### Exception hierarchy

```
OnPremError                            # base
+- AuthenticationError                 # 401
+- ValidationError                     # 422
+- BusyError                           # 503 after retries exhausted
+- CancelledError                      # 499
+- ServerBusyDuringSolve                # network drop during long solve
+- NotImplementedOnServer              # 501 (optimal-bidding stub)
+- TimeoutError                        # client-side timeout
+- ServerError                         # 5xx other than 503
+- IdempotencyConflict                 # only if server returns specific code (future)
```

All exceptions carry `.code`, `.message`, `.details` populated from the server's structured error envelope (defined in `server-onprem/docs/SPEC.md` section 3.8).

### Auth

Single mechanism: bearer token in `Authorization` header. `OnPremClient` stores the key from constructor or reads from `SITE_CALC_ONPREM_API_KEY` env var. Never logs the key.

### 503 retry policy

Default: 3 retries, exponential backoff with jitter, `initial=10s` then doubling, capped at `max_delay=60s`. Honor the server's `Retry-After` header if present (overrides backoff math). Configurable via `BackoffPolicy`. Set `busy_retry=None` to disable retries and surface `BusyError` immediately.

### Idempotency

Caller passes `idempotency_key="<opaque-string>"` to `device_planning(...)`. The client sets `Idempotency-Key: <value>` and surfaces `X-Idempotent-Replay: true` from the response on a `Run` attribute (`run.replayed_from_idempotency_key`).

### Versioning & negotiation

On construction, `OnPremClient` calls `GET /v1/health` once and records `site_calc_version`, `site_calc_commit_sha`, and `service_version`. If the SDK's compile-time-known compatible range does not include the server's version, log a warning. Do not refuse to connect (server-side schema is the source of truth; client is permissive).

### Pydantic models

Reuse `site_calc_operational.models.*` for `DevicePlanningRequest`, `DevicePlanningResponse`, etc. These are **already** the same shapes the on-prem server accepts. The schema-drift test (`server-onprem/tests/test_schema_drift.py`) catches divergence.

## Phasing

The SDK work runs **in parallel** with server-onprem phases G1-G6. SDK gates:

| Phase | Scope | Integration gate |
|---|---|---|
| **C1 - Skeleton** | New module file, `OnPremClient` class with constructor + `health()` only, exception hierarchy | Unit test: pointed at a stubbed FastAPI in tests/ returning fake health, parses correctly |
| **C2 - Sync solve** | `device_planning()`, 503 backoff, error parsing, idempotency-key passthrough | Integration test: spin up real `server-onprem` via docker-compose, call `device_planning()` against a fixture site, assert objective in tolerance |
| **C3 - Read-back & cancel** | `get_run()`, `list_runs()`, `cancel_active()` | Integration test: cancel mid-solve via SDK; verify exception type and persisted run shape |
| **C4 - Optimal-bidding stub** | `optimal_bidding()` raises `NotImplementedOnServer` cleanly | Integration test: assert the right exception with the right code |
| **C5 - Polish** | README example, type-stubs check, version bump | `pip install site-calc-operational==<new>` works; existing async client unchanged |

Each C-phase has a server-side counterpart (C1 needs G1, C2 needs G3, C3 needs G4, C4 needs G5, C5 needs G6). C-phases ship after their server pair.

## Versioning

This addition is a **minor** version bump on `site-calc-operational` (e.g., 1.2.x -> 1.3.0). Adding a new class does not break existing async-client users.
