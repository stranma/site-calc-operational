"""FastMCP server exposing operational device-planning tools.

This module is the entry point for ``python -m site_calc_operational.mcp``
and the ``site-calc-operational-mcp`` console script (provided by the ``mcp``
extra in ``pyproject.toml``).

The server lets an LLM:
- assemble a multi-device operational scenario incrementally
  (``create_scenario``, ``add_device``, ``set_timespan``, ...);
- submit it for a synchronous solve against a self-hosted ``server-onprem``
  (``solve``);
- inspect, replay, or cancel runs (``get_run``, ``list_runs``, ``cancel_active``);
- save/fetch profile data the LLM has generated or sourced from URLs
  (``save_data_file``, ``fetch_url``).

The on-prem server URL and API key are read from environment variables (see
:class:`Config`). All tools call into a single :class:`OnPremClient` instance
managed by :func:`_get_client`.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from site_calc_operational import __version__
from site_calc_operational.api.onprem_client import OnPremClient
from site_calc_operational.api.onprem_exceptions import OnPremError
from site_calc_operational.mcp.config import Config, get_data_dir
from site_calc_operational.mcp.data_loaders import fetch_url_to_file, save_csv
from site_calc_operational.mcp.scenario import (
    OperationalScenarioStore,
    known_device_types,
)
from site_calc_operational.mcp.scenario import (
    get_device_schema as _scenario_device_schema,
)

mcp = FastMCP(
    "site-calc-operational",
    instructions=(
        "Operational device-planning tools for self-hosted site-calc deployments. "
        "Build a single-site scenario (devices, timespan, solver config), then "
        "submit a synchronous solve against an on-prem server.\n\n"
        "Workflow:\n"
        "  1. create_scenario(name, site_id)\n"
        "  2. add_device(...) for each device\n"
        "  3. set_timespan(period_start, period_end, resolution)\n"
        "  4. (optional) set_optimization_config(...)\n"
        "  5. review_scenario -> verify validation.valid is True\n"
        "  6. solve -> returns optimization summary + run_id\n\n"
        "Profile-shaped properties (price, demand_profile, generation_profile, etc.) "
        "accept a scalar (broadcast to the timespan), an explicit list, or a file "
        'reference {"file": "<path>", "column": "<name>"}. Use save_data_file '
        "to persist generated arrays to CSV first, then reference them in add_device. "
        "Use fetch_url to download remote CSVs."
    ),
)

_store = OperationalScenarioStore()
_client: OnPremClient | None = None


def _get_client() -> OnPremClient:
    """Get-or-create the singleton :class:`OnPremClient`.

    The client is built from environment variables on first use. Raises if the
    required ``SITE_CALC_OPERATIONAL_API_KEY`` env var is missing.

    :returns: Process-wide :class:`OnPremClient`.
    :raises ValueError: ``SITE_CALC_OPERATIONAL_API_KEY`` is unset.
    """
    global _client
    if _client is None:
        cfg = Config.from_env()
        _client = OnPremClient(base_url=cfg.api_url, api_key=cfg.api_key)
    return _client


def _set_client_for_tests(client: OnPremClient | None) -> None:
    """Inject a client (test hook). Pass ``None`` to reset the singleton."""
    global _client
    _client = client


def _reset_store_for_tests() -> None:
    """Replace the module-level store with a fresh one (test hook)."""
    global _store
    _store = OperationalScenarioStore()


# ---------------------------------------------------------------------------
# Server-info tools
# ---------------------------------------------------------------------------


def get_version() -> dict[str, Any]:
    """Return the client package version and (if reachable) the server version.

    The server-side fields require a valid API key. If the server is unreachable
    or auth fails, the result still includes ``client_version`` plus an
    ``error`` field describing what failed -- this lets the LLM differentiate
    "server down" from "client misconfigured".

    :returns: Dict with at least ``client_version``; on success also
        ``server_status``, ``server_site_calc_version``, ``server_commit_sha``,
        ``server_db_ok``, ``compatible``.
    """
    out: dict[str, Any] = {"client_version": __version__}
    try:
        info = _get_client().health()
    except (OnPremError, ValueError) as exc:
        out["error"] = str(exc)
        return out
    except Exception as exc:  # network failure, etc.
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    out["server_status"] = info.status
    out["server_site_calc_version"] = info.site_calc_version
    out["server_commit_sha"] = info.site_calc_commit_sha
    out["server_db_ok"] = info.db_ok
    out["server_active_solve"] = info.active_solve
    out["service_version"] = info.service_version
    client_minor = ".".join(__version__.split(".")[:2])
    server_minor = ".".join(info.service_version.split(".")[:2])
    out["compatible"] = client_minor == server_minor
    return out


def health() -> dict[str, Any]:
    """Probe the on-prem server's ``/v1/health`` endpoint.

    Useful as a quick sanity check that the URL/API key combination works
    before attempting a (potentially long-running) solve.

    :returns: Dict mirroring the server's HealthInfo payload.
    """
    info = _get_client().health()
    return {
        "status": info.status,
        "site_calc_version": info.site_calc_version,
        "site_calc_commit_sha": info.site_calc_commit_sha,
        "service_version": info.service_version,
        "db_ok": info.db_ok,
        "active_solve": info.active_solve,
    }


# ---------------------------------------------------------------------------
# Scenario assembly
# ---------------------------------------------------------------------------


def create_scenario(name: str, site_id: str, description: str = "") -> dict[str, str]:
    """Start a new draft device-planning scenario.

    :param name: Human-readable label for the LLM's own bookkeeping.
    :param site_id: Server-side site identifier (passed through to the request).
    :param description: Optional longer description.
    :returns: Dict with ``scenario_id``, ``name``, ``site_id``.
    """
    sid = _store.create(name=name, site_id=site_id, description=description)
    return {"scenario_id": sid, "name": name, "site_id": site_id}


def add_device(
    scenario_id: str,
    name: str,
    device_type: str,
    properties: dict[str, Any],
    schedule: dict[str, Any] | None = None,
) -> str:
    """Append a device to a draft scenario.

    Use :func:`get_device_schema` to discover required / optional properties for
    a device type. Profile-shaped properties (price, demand_profile, ...) accept
    one of:

    - ``50.0`` -- scalar broadcast to ``intervals`` length at solve time;
    - ``[55.0, 50.0, ...]`` -- explicit array (must match ``intervals``);
    - ``{"file": "prices.csv"}`` -- load first numeric column from CSV;
    - ``{"file": "prices.csv", "column": "price_eur"}`` -- specific CSV column;
    - ``{"file": "prices.json"}`` -- flat JSON numeric array.

    :param scenario_id: Target scenario.
    :param name: Unique device name within the scenario.
    :param device_type: Lowercase device type string (see :func:`get_device_schema`).
    :param properties: Device-type-specific dict (required + optional fields).
    :param schedule: Optional runtime constraints; only valid for device types
        with ``supports_schedule == True``.
    :returns: Confirmation message.
    """
    return _store.add_device(
        scenario_id=scenario_id,
        name=name,
        device_type=device_type,
        properties=properties,
        schedule=schedule,
    )


def remove_device(scenario_id: str, device_name: str) -> str:
    """Remove a previously-added device from a scenario.

    :param scenario_id: Target scenario.
    :param device_name: Name of the device to drop.
    :returns: Confirmation message.
    """
    _store.remove_device(scenario_id=scenario_id, device_name=device_name)
    return f"Removed device '{device_name}' from scenario {scenario_id}."


def set_timespan(scenario_id: str, period_start: str, period_end: str, resolution: str = "1h") -> str:
    """Set the operational solve horizon.

    Operational horizons are short (typically 1-7 days at 15min-1h resolution).
    The on-prem server's solver time-limit is also bounded -- prefer 24-72 hour
    horizons unless you know the LP solves quickly.

    :param scenario_id: Target scenario.
    :param period_start: ISO-8601 datetime, timezone-aware (e.g. ``"2026-05-01T00:00:00+00:00"``).
    :param period_end: ISO-8601 datetime, exclusive end of horizon. Must be > ``period_start``.
    :param resolution: ``"15min"``, ``"30min"``, or ``"1h"`` (default).
    :returns: Confirmation including derived interval count.
    """
    return _store.set_timespan(
        scenario_id=scenario_id,
        period_start=period_start,
        period_end=period_end,
        resolution=resolution,
    )


def set_optimization_config(
    scenario_id: str,
    objective: str | None = None,
    time_limit_seconds: int | None = None,
    mip_gap: float | None = None,
    solver: str | None = None,
) -> str:
    """Override the default solver/objective config (only non-None args take effect).

    Defaults are ``objective="maximize_profit"``, ``time_limit_seconds=120``,
    ``mip_gap=0.01``, ``solver="highs"``. The on-prem server caps
    ``time_limit_seconds`` at 600.

    :param scenario_id: Target scenario.
    :param objective: ``"maximize_profit"`` or ``"minimize_cost"``.
    :param time_limit_seconds: Solver wall-clock cap in seconds (1-600).
    :param mip_gap: Optimality gap in [0, 1]; smaller is tighter.
    :param solver: ``"highs"`` (always available) | ``"cbc"`` | ``"gurobi"`` | ``"cplex"``.
    :returns: Confirmation with effective config.
    """
    return _store.set_optimization_config(
        scenario_id=scenario_id,
        objective=objective,
        time_limit_seconds=time_limit_seconds,
        mip_gap=mip_gap,
        solver=solver,
    )


def review_scenario(scenario_id: str) -> dict[str, Any]:
    """Return a summary of a scenario plus a validation verdict.

    Inspect ``validation.valid`` before calling :func:`solve`. If it's False,
    ``validation.errors`` lists the issues (missing timespan, profile length
    mismatch, etc.).
    """
    return _store.review(scenario_id=scenario_id)


def delete_scenario(scenario_id: str) -> str:
    """Drop a draft scenario from the store. Idempotent on unknown ids."""
    _store.delete(scenario_id=scenario_id)
    return f"Deleted scenario {scenario_id} (if it existed)."


def list_scenarios() -> list[dict[str, Any]]:
    """Return summaries of all draft scenarios currently in the store.

    Each entry contains ``scenario_id``, ``name``, ``site_id``, ``device_count``,
    ``has_timespan``, ``run_count``.
    """
    return [
        {
            "scenario_id": s.id,
            "name": s.name,
            "site_id": s.site_id,
            "device_count": s.device_count,
            "has_timespan": s.has_timespan,
            "run_count": len(s.runs),
        }
        for s in _store.list()
    ]


# ---------------------------------------------------------------------------
# Solving + run inspection
# ---------------------------------------------------------------------------


def solve(scenario_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
    """Run an operational device-planning solve synchronously.

    Builds the request payload from the draft scenario, submits it, and returns
    a compact summary (full schedules are persisted in the run record on the
    server -- use :func:`get_run` to fetch them). The same scenario can be
    solved multiple times; each call appends a new ``run_id`` to its history.

    :param scenario_id: Scenario to solve.
    :param idempotency_key: Optional client-supplied key. If the server has a
        cached response for this key (within the last 24h), it is replayed
        without rerunning the solver.
    :returns: Dict with ``run_id``, ``solver_status``, ``summary`` (server-side
        summary), and ``replay`` (bool indicating an idempotent replay).
    :raises KeyError: Unknown ``scenario_id``.
    :raises ValueError: Scenario fails validation (call :func:`review_scenario`).
    :raises OnPremError: Server returned a non-200 status.
    """
    payload = _store.build_request(scenario_id)
    response = _get_client().device_planning(payload, idempotency_key=idempotency_key)

    run_id = response.get("run_id", "")
    if run_id:
        _store.record_run(scenario_id, run_id)
    return {
        "scenario_id": scenario_id,
        "run_id": run_id,
        "solver_status": response.get("summary", {}).get("solver_status", "unknown"),
        "summary": response.get("summary", {}),
    }


def get_run(run_id: str) -> dict[str, Any]:
    """Fetch a complete run record (request + response + timing) by id.

    :param run_id: UUID returned by :func:`solve` or by ``list_runs``.
    :returns: Server's full run dict.
    :raises OnPremError: 404 (unknown id or owned by a different user) or other.
    """
    return _get_client().get_run(run_id)


def list_runs(
    endpoint: str | None = None,
    status: str | None = None,
    limit: int = 50,
    before: str | None = None,
) -> dict[str, Any]:
    """List the caller's recent runs, newest first.

    :param endpoint: ``"device-planning"`` or ``"optimal-bidding"`` to filter; default both.
    :param status: ``"ok"``, ``"error"``, or ``"cancelled"`` to filter; default any.
    :param limit: Max rows (server caps at 200; default 50).
    :param before: ISO-8601 cursor for pagination (use the previous response's
        ``next_before`` field).
    :returns: Dict ``{runs: [...], next_before: str | null}``.
    """
    return _get_client().list_runs(endpoint=endpoint, status=status, limit=limit, before=before)


def cancel_active() -> dict[str, Any]:
    """Cancel the currently-running solve, if any.

    The on-prem server runs at most one solve at a time. If a solve is in
    flight, this signals it to abort and returns the (now-cancelled) run record.
    If the slot is idle the server returns 204; this tool surfaces that as
    ``{"cancelled": false, ...}`` so the LLM can branch on it.

    :returns: Dict with ``cancelled`` (bool) and the cancelled run record (if any).
    """
    cancelled_run = _get_client().cancel_active()
    if cancelled_run is None:
        return {"cancelled": False, "message": "No solve was active."}
    return {"cancelled": True, "run": cancelled_run}


# ---------------------------------------------------------------------------
# Schema helper
# ---------------------------------------------------------------------------


def get_device_schema(device_type: str) -> dict[str, Any]:
    """Describe required and optional properties for a device type.

    :param device_type: e.g. ``"battery"``, ``"chp"``, ``"electricity_import"``.
    :returns: Dict with ``device_type``, ``required`` (name->kind), ``optional``
        (name->kind), and ``supports_schedule``. ``kind`` is ``"scalar"``,
        ``"profile"``, or ``"object"``. For unknown types, the dict has
        ``error`` and ``valid_types``.
    """
    schema = _scenario_device_schema(device_type)
    if schema is None:
        return {"error": f"Unknown device type '{device_type}'.", "valid_types": known_device_types()}
    return {
        "device_type": device_type.lower(),
        "required": dict(schema["required"]),
        "optional": dict(schema["optional"]),
        "supports_schedule": schema["supports_schedule"],
    }


# ---------------------------------------------------------------------------
# Data file helpers
# ---------------------------------------------------------------------------


def save_data_file(file_path: str, columns: dict[str, list[float]], overwrite: bool = False) -> dict[str, Any]:
    """Write generated profile data to a local CSV file.

    LLMs cannot touch the user's filesystem directly, but the MCP server runs
    on the user's machine and can. Use this to persist arrays so they can be
    referenced later by ``add_device({..., "price": {"file": "<path>", "column": "<name>"}})``.

    :param file_path: Destination filename. Relative paths resolve against
        ``SITE_CALC_OPERATIONAL_DATA_DIR`` env var (or cwd). ``.csv`` is appended
        if missing.
    :param columns: Mapping ``{column_name: [numeric_values, ...]}``. All
        columns must have the same length.
    :param overwrite: Allow replacing an existing file.
    :returns: Dict with ``file_path`` (absolute), ``columns``, ``rows``, ``message``.
    """
    abs_path = save_csv(file_path=file_path, columns=columns, data_dir=get_data_dir(), overwrite=overwrite)
    rows = len(next(iter(columns.values())))
    return {
        "file_path": abs_path,
        "columns": list(columns.keys()),
        "rows": rows,
        "message": f"Saved {rows} rows to {abs_path}",
    }


def fetch_url(url: str, file_path: str | None = None, overwrite: bool = False) -> dict[str, Any]:
    """Download a remote file (CSV or other) to the local filesystem.

    For CSV downloads, the response body is parsed and the metadata in the
    return value tells the LLM how many rows / columns there are -- so it can
    immediately call ``set_timespan(intervals=rows)`` and reference the file
    in subsequent ``add_device`` calls.

    :param url: HTTP/HTTPS URL.
    :param file_path: Local filename. Default: derived from URL path.
    :param overwrite: Allow replacing an existing file.
    :returns: Dict with ``file_path``, ``url``, ``bytes``, plus ``rows`` /
        ``columns`` / ``numeric_columns`` for CSV.
    """
    return fetch_url_to_file(url=url, data_dir=get_data_dir(), file_path=file_path, overwrite=overwrite)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

mcp.tool()(get_version)
mcp.tool()(health)
mcp.tool()(create_scenario)
mcp.tool()(add_device)
mcp.tool()(remove_device)
mcp.tool()(set_timespan)
mcp.tool()(set_optimization_config)
mcp.tool()(review_scenario)
mcp.tool()(delete_scenario)
mcp.tool()(list_scenarios)
mcp.tool()(solve)
mcp.tool()(get_run)
mcp.tool()(list_runs)
mcp.tool()(cancel_active)
mcp.tool()(get_device_schema)
mcp.tool()(save_data_file)
mcp.tool()(fetch_url)


def main() -> None:
    """Console entry point. Runs the FastMCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
