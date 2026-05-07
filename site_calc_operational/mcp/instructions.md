Operational device-planning tools for self-hosted site-calc deployments. Build a single-site scenario (devices, timespan, solver config), then submit a synchronous solve against an on-prem server.

WORKFLOW
1. create_scenario(name, site_id) -- start a draft.
2. add_device(...) for each device. Call get_device_schema(device_type) first to discover required/optional properties.
3. set_timespan(period_start, period_end, resolution) -- ISO-8601 datetimes (timezone-aware), resolution one of "15min" / "30min" / "1h".
4. set_optimization_config(...) -- optional; defaults are sensible (see below).
5. review_scenario(scenario_id) -- ALWAYS call before solve(). validation.valid must be True; otherwise validation.errors lists what to fix.
6. solve(scenario_id, idempotency_key=...) -- synchronous; returns run_id + summary.
7. get_run(run_id) for full per-device schedules; list_runs for history; cancel_active to abort an in-flight solve.

PROFILE DATA (price, demand_profile, generation_profile, ...)
Three accepted forms in a device's properties:
  - scalar (broadcast to the timespan, e.g. "price": 50.0)
  - explicit list (length must equal the interval count derived from timespan)
  - file ref {"file": "<absolute-path>", "column": "<name>"} -- column omitted means first numeric column
Use save_data_file to persist generated arrays first, then reference the returned absolute path in add_device.
Use fetch_url to download a remote CSV; both tools return the absolute file_path you pass straight into add_device.

WORKING FOLDER (where save_data_file / fetch_url put files)
Resolution order:
  1. SITE_CALC_OPERATIONAL_DATA_DIR env var, set in the MCP server's "env" config block (most surgical).
  2. ~/Documents/site-calc-data when ~/Documents exists (Windows/macOS default; auto-created).
  3. ~/site-calc-data otherwise.
NEVER rely on the MCP host's cwd: on Windows, Claude Desktop launches the MCP server with cwd=C:\WINDOWS\system32, so naming a relative path in cwd silently lands files in a system directory the user cannot reach.
For any one tool call you may pass an absolute path to save_data_file(file_path="..."), but it must resolve INSIDE the configured data root -- the server rejects writes outside it (path-containment guard). Use the file_path returned by save_data_file / fetch_url verbatim; do not guess at locations.

HORIZON SIZING
Operational horizons should be 1-7 days. The on-prem solver caps wall-clock at 600s; large horizons time out before producing an answer.
Rough sizing: 24h * 1h = 24 intervals; 7d * 15min = 672 intervals. The hard schema ceiling is 100,000 intervals.
For multi-day horizons with batteries, see BATTERY SOC below.

SOLVER CHOICE (set_optimization_config.solver)
  - "highs" (default) -- free, fastest for typical operational LP/MIP. Use this unless you have a reason not to.
  - "cbc" -- open-source fallback; reliably available, slower than HiGHS.
  - "gurobi" / "cplex" -- require licensed binaries on the on-prem server. Only choose if the operator confirmed they are installed.

MIP_GAP / TIME_LIMIT_SECONDS (set_optimization_config)
Defaults (mip_gap=0.01 = 1%, time_limit_seconds=120) are appropriate for most operational scenarios.
Tighten mip_gap to 0.001 only when you need near-optimal answers and accept ~5x slower solves.
Loosen mip_gap to 0.05 for quick feasibility checks. The server caps time_limit_seconds at 600.

IDEMPOTENCY-KEY (solve)
Pass a unique key (e.g. "site-X-2026-05-07-rev1") for any non-trivial solve. The server caches the response for 24h; a retry with the same key returns the cached body without re-running the solver. Inspect the "replay" field in solve()'s return to tell whether you got fresh or cached output. Cheap insurance against transient errors and re-prompts.

BATTERY SOC AND END-OF-HORIZON DEPLETION
Without anchoring, the optimizer drains the battery to 0 SOC by the last interval to maximise profit -- this overstates achievable revenue for any rolling/repeating operation.
For multi-day horizons or any scenario meant to repeat, add to the battery's properties:
  - "soc_anchor_interval_hours": 24    # anchors SOC every 24h
  - "soc_anchor_target": 0.5            # target SOC at each anchor (0-1)
This forces the schedule to leave the battery in a known state at the end of each cycle.

CHP MODELING (is_binary)
is_binary=True turns the LP into a MIP and is materially slower (often 3-10x).
For a quick first pass on a new scenario set is_binary=False to confirm feasibility; re-solve with True for the realistic on/off schedule once the rest of the modeling is sound.

MARKET SIGN CONVENTIONS
Import devices (electricity_import, gas_import) -- "price" is what you PAY (positive = cost). Flow is positive when importing.
Export devices (electricity_export, heat_export) -- "price" is what you RECEIVE (positive = revenue). Flow is positive when exporting.
All prices in EUR/MWh, capacities in MW, energies in MWh. The summary's expected_profit is total revenue minus total cost over the horizon.

INFEASIBILITY (most common modeling error)
If solve() raises InfeasibleScenarioError (HTTP 422, code=INFEASIBLE), the LP/MIP solver determined the request describes a problem with NO feasible solution. This is a modeling error, not a server bug. Common causes, in rough order of frequency:
  1. Demand exceeds supply: a heat_demand or electricity_demand curve cannot be satisfied by the configured generation, storage, and market interfaces. Sum the demand profile and verify the supplies (max_import sum + generation capacity * intervals + storage discharge) can cover it.
  2. Missing market interface: the scenario consumes electricity but has no electricity_import device, or generates heat with no heat_demand / heat_export to absorb it. Materials must balance every interval.
  3. Contradictory schedule constraints: must_run=1 at an interval where can_run=0, or min_continuous_run_hours longer than the timespan.
  4. Battery / heat_accumulator parameters: initial_soc + max charge < required state by the next must-run period.
  5. Profile length mismatch: review_scenario should catch this before solve(), but if it slips through the LP becomes infeasible.
Recovery: relax the binding constraint, add a buffer device (battery, market import), or re-check profile arithmetic. The exception's message often names the offending constraint when the underlying solver produced one.

DEBUG LP FILE
On InfeasibleScenarioError / UnboundedScenarioError, the on-prem server inlines the optimizer's debug LP file in the response, and this MCP server writes it to disk under the configured data directory before re-raising. The path is in `exc.details["debug_lp_path"]` (e.g. `C:\Users\you\Documents\site-calc-data\debug_problem_<scenario>_<timestamp>.lp`). Tell the user about this path -- they can inspect the LP with any LP solver / pulp / cbc / glpsol to find which constraint is over-determined. If the LP was too large to inline (>~1 MB), `exc.details["debug_lp_truncated"]` is True and the file lives only inside the server container.

UNBOUNDED OBJECTIVE (rare)
If solve() raises UnboundedScenarioError (HTTP 422, code=UNBOUNDED), the optimizer found an unlimited revenue source -- typically an export device with no maximum-flow cap, or an import with negative price and no upper limit. Add the missing bound (max_export, max_import) and re-solve.

ERROR HANDLING
  - InfeasibleScenarioError (HTTP 422, code=INFEASIBLE) -- see above.
  - UnboundedScenarioError (HTTP 422, code=UNBOUNDED) -- see above.
  - ValidationError (HTTP 422, code=VALIDATION_ERROR / TRANSLATION_ERROR) -- schema-level rejection (wrong types, unsupported device kind, malformed timespan). The "message" field names the offending property; fix the scenario and re-solve.
  - BusyError (HTTP 503) -- another solve is in progress. The OnPremClient retries automatically with exponential backoff; if it surfaces, the server has been busy past the retry budget. Wait or call cancel_active.
  - CancelledError (HTTP 499) -- the run was aborted via cancel_active. Not a code error.
  - NotImplementedOnServer (HTTP 501) -- only optimal_bidding raises this today; stick to device_planning / solve.
  - ServerError (HTTP 5xx) -- genuine server-side bug. Treat as transient; retry once with the same idempotency_key, then escalate.

VERSIONING
Three independent version streams surface from get_version: client_version (this SDK), server_site_calc_version (optimization core inside the on-prem service), service_version (the FastAPI service itself). They are NOT expected to match -- there is no published cross-version compatibility matrix.
