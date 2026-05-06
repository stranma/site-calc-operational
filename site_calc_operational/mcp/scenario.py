"""In-memory scenario builder for operational device-planning payloads.

A :class:`Scenario` holds the partial state of a device-planning request as the
LLM assembles it via tool calls. Once the timespan, devices, and config are
all set, :meth:`OperationalScenarioStore.build_request` materializes a payload
that matches the on-prem server's ``DevicePlanningRequest`` schema.

Profile-shaped properties (price, demand_profile, etc.) accept three forms in
the stored draft and are resolved to flat arrays at build time:

- scalar  -> broadcast to ``intervals`` length
- list    -> validated length
- ``{"file": "<path>", "column": "<name>"}`` -> loaded via data_loaders
"""

from __future__ import annotations

import datetime as dt
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from site_calc_operational.mcp.data_loaders import resolve_profile

# ---------------------------------------------------------------------------
# Device schemas
# ---------------------------------------------------------------------------

# Each schema describes a device type's properties:
#   "scalar"  -> required scalar (float/int/bool); not broadcast
#   "profile" -> required profile (scalar/list/file); resolved at build_request
#   "object"  -> required structured value (e.g. {latitude, longitude}); passed through
#   "optional_*" -> as above but not required
_DEVICE_SCHEMAS: dict[str, dict[str, Any]] = {
    "battery": {
        "required": {"capacity": "scalar", "max_power": "scalar", "efficiency": "scalar"},
        "optional": {"initial_soc": "scalar", "soc_anchor_interval_hours": "scalar", "soc_anchor_target": "scalar"},
        "supports_schedule": True,
    },
    "heat_accumulator": {
        "required": {"capacity": "scalar", "max_power": "scalar", "efficiency": "scalar"},
        "optional": {"initial_soc": "scalar", "loss_rate": "scalar"},
        "supports_schedule": True,
    },
    "chp": {
        "required": {"gas_input": "scalar", "el_output": "scalar", "heat_output": "scalar"},
        "optional": {"is_binary": "scalar", "min_power": "scalar"},
        "supports_schedule": True,
    },
    "photovoltaic": {
        "required": {"peak_power_mw": "scalar"},
        "optional": {
            "location": "object",
            "tilt": "scalar",
            "azimuth": "scalar",
            "generation_profile": "profile",
        },
        "supports_schedule": True,
    },
    "electricity_import": {
        "required": {"max_import": "scalar", "price": "profile"},
        "optional": {"max_import_unit_cost": "scalar"},
        "supports_schedule": False,
    },
    "electricity_export": {
        "required": {"max_export": "scalar", "price": "profile"},
        "optional": {"max_export_unit_cost": "scalar"},
        "supports_schedule": False,
    },
    "gas_import": {
        "required": {"max_import": "scalar", "price": "profile"},
        "optional": {"max_import_unit_cost": "scalar"},
        "supports_schedule": False,
    },
    "heat_export": {
        "required": {"max_export": "scalar", "price": "profile"},
        "optional": {"max_export_unit_cost": "scalar"},
        "supports_schedule": False,
    },
    "electricity_demand": {
        "required": {"demand_profile": "profile"},
        "optional": {"min_demand_profile": "profile", "max_demand_profile": "profile"},
        "supports_schedule": False,
    },
    "heat_demand": {
        "required": {"demand_profile": "profile"},
        "optional": {"min_demand_profile": "profile", "max_demand_profile": "profile"},
        "supports_schedule": False,
    },
}


def get_device_schema(device_type: str) -> dict[str, Any] | None:
    """Return the property schema for ``device_type`` or ``None`` if unknown.

    :param device_type: Lowercase device type string (e.g. ``"battery"``).
    :returns: Schema dict (with ``required`` / ``optional`` / ``supports_schedule``)
        or ``None`` for unknown types.
    """
    return _DEVICE_SCHEMAS.get(device_type.lower())


def known_device_types() -> list[str]:
    """List the supported device type strings, sorted alphabetically."""
    return sorted(_DEVICE_SCHEMAS.keys())


_VALID_RESOLUTIONS = {"15min": dt.timedelta(minutes=15), "30min": dt.timedelta(minutes=30), "1h": dt.timedelta(hours=1)}
_VALID_OBJECTIVES = {"maximize_profit", "minimize_cost"}
_VALID_SOLVERS = {"highs", "cbc", "gurobi", "cplex"}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Timespan:
    """Solve horizon in operational coordinates.

    :param period_start: ISO-8601 datetime (timezone-aware).
    :param period_end: ISO-8601 datetime (exclusive, must be > period_start).
    :param resolution: One of ``"15min"``, ``"30min"``, ``"1h"``.
    """

    period_start: str
    period_end: str
    resolution: str

    @property
    def intervals(self) -> int:
        """Number of intervals between ``period_start`` and ``period_end``.

        :returns: Positive integer interval count.
        :raises ValueError: ``resolution`` is unknown or the horizon is not an
            integer multiple of the resolution.
        """
        if self.resolution not in _VALID_RESOLUTIONS:
            raise ValueError(f"Unknown resolution '{self.resolution}'; expected one of {sorted(_VALID_RESOLUTIONS)}.")
        delta = _VALID_RESOLUTIONS[self.resolution]
        start = dt.datetime.fromisoformat(self.period_start)
        end = dt.datetime.fromisoformat(self.period_end)
        if end <= start:
            raise ValueError(f"period_end ({self.period_end}) must be after period_start ({self.period_start}).")
        seconds = (end - start).total_seconds()
        step = delta.total_seconds()
        if seconds % step != 0:
            raise ValueError(
                f"Horizon length ({seconds}s) is not a whole multiple of resolution '{self.resolution}' ({step}s)."
            )
        return int(seconds // step)


@dataclass
class OptimizationConfig:
    """Solver options for a device-planning request.

    :param objective: ``"maximize_profit"`` or ``"minimize_cost"``.
    :param time_limit_seconds: Solver wall-clock cap (server caps at 600).
    :param mip_gap: Optimality gap (e.g. ``0.01`` = 1%). 0 means solve to optimality.
    :param solver: Solver backend (``"highs"`` is the default and the only one
        the on-prem server is guaranteed to ship with).
    """

    objective: str = "maximize_profit"
    time_limit_seconds: int = 120
    mip_gap: float = 0.01
    solver: str = "highs"


@dataclass
class Scenario:
    """A draft device-planning scenario being assembled by tool calls.

    Mutable until :meth:`OperationalScenarioStore.build_request` is called and
    the resulting payload is sent to the server. The scenario itself is never
    erased on solve, so the LLM can re-solve with adjusted parameters.
    """

    id: str
    name: str
    site_id: str
    description: str = ""
    devices: list[dict[str, Any]] = field(default_factory=list)
    timespan: Timespan | None = None
    optimization_config: OptimizationConfig = field(default_factory=OptimizationConfig)
    runs: list[str] = field(default_factory=list)  # run_ids returned by /v1/device-planning

    @property
    def device_count(self) -> int:
        """Number of devices currently attached."""
        return len(self.devices)

    @property
    def has_timespan(self) -> bool:
        """Whether ``timespan`` has been set."""
        return self.timespan is not None


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class OperationalScenarioStore:
    """In-memory scenario registry. Not threadsafe; the MCP server is single-process.

    The store is the source of truth for the in-flight assembly state. It does
    not persist anything to disk -- restarting the MCP server discards all
    drafts (but persisted runs on the on-prem server are unaffected).
    """

    def __init__(self) -> None:
        """Initialise an empty store."""
        self._scenarios: dict[str, Scenario] = {}

    # -------- CRUD --------------------------------------------------------

    def create(self, name: str, site_id: str, description: str = "") -> str:
        """Create a fresh draft scenario.

        :param name: Human-readable scenario name.
        :param site_id: Site identifier for the single-site solve.
        :param description: Optional longer description.
        :returns: New scenario id.
        :raises ValueError: ``name`` or ``site_id`` is empty/whitespace.
        """
        if not name or not name.strip():
            raise ValueError("Scenario name must be non-empty.")
        if not site_id or not site_id.strip():
            raise ValueError("site_id must be non-empty.")
        sid = str(uuid.uuid4())
        self._scenarios[sid] = Scenario(id=sid, name=name.strip(), site_id=site_id.strip(), description=description)
        return sid

    def get(self, scenario_id: str) -> Scenario:
        """Fetch the scenario with id ``scenario_id``.

        :raises KeyError: Unknown scenario id.
        """
        try:
            return self._scenarios[scenario_id]
        except KeyError as exc:
            raise KeyError(f"Unknown scenario_id '{scenario_id}'.") from exc

    def delete(self, scenario_id: str) -> None:
        """Delete a scenario; idempotent on missing ids.

        :param scenario_id: Scenario to drop.
        """
        self._scenarios.pop(scenario_id, None)

    def list(self) -> list[Scenario]:
        """Return all scenarios, oldest first."""
        return list(self._scenarios.values())

    # -------- Mutation helpers -------------------------------------------

    def add_device(
        self,
        scenario_id: str,
        name: str,
        device_type: str,
        properties: dict[str, Any],
        schedule: dict[str, Any] | None = None,
    ) -> str:
        """Append a device to the scenario.

        :param scenario_id: Target scenario.
        :param name: Device name; must be unique within the scenario.
        :param device_type: One of :func:`known_device_types`.
        :param properties: Device-type-specific properties (see :func:`get_device_schema`).
        :param schedule: Optional schedule dict (only for devices with ``supports_schedule``).
        :returns: Confirmation message.
        :raises KeyError: Unknown scenario id.
        :raises ValueError: Device name conflict, unknown type, missing required property,
            or schedule supplied for a non-supporting device.
        """
        scenario = self.get(scenario_id)
        if not name or not name.strip():
            raise ValueError("Device name must be non-empty.")
        device_type_norm = device_type.lower()
        schema = get_device_schema(device_type_norm)
        if schema is None:
            raise ValueError(f"Unknown device type '{device_type}'. Known: {known_device_types()}.")
        if any(d["name"] == name for d in scenario.devices):
            raise ValueError(f"Device name '{name}' already exists in scenario {scenario_id}.")
        missing = [p for p in schema["required"] if p not in properties]
        if missing:
            raise ValueError(f"Missing required properties for {device_type_norm}: {missing}")
        if schedule is not None and not schema["supports_schedule"]:
            raise ValueError(f"Device type '{device_type_norm}' does not support schedule.")

        device: dict[str, Any] = {
            "name": name.strip(),
            "type": device_type_norm,
            "properties": deepcopy(properties),
        }
        if schedule is not None:
            device["schedule"] = deepcopy(schedule)
        scenario.devices.append(device)
        return (
            f"Added {device_type_norm} '{name.strip()}' to scenario {scenario.name!r} ({scenario.device_count} total)."
        )

    def remove_device(self, scenario_id: str, device_name: str) -> None:
        """Remove a device by name.

        :raises ValueError: Device not present.
        """
        scenario = self.get(scenario_id)
        before = len(scenario.devices)
        scenario.devices = [d for d in scenario.devices if d["name"] != device_name]
        if len(scenario.devices) == before:
            raise ValueError(f"Device '{device_name}' not found in scenario {scenario_id}.")

    def set_timespan(self, scenario_id: str, period_start: str, period_end: str, resolution: str) -> str:
        """Set the solve horizon. ``period_start``/``period_end`` must be ISO-8601.

        :returns: Confirmation including the derived interval count.
        :raises ValueError: Invalid datetimes / resolution / non-whole horizon.
        """
        scenario = self.get(scenario_id)
        ts = Timespan(period_start=period_start, period_end=period_end, resolution=resolution)
        intervals = ts.intervals  # raises if invalid
        scenario.timespan = ts
        return f"Set timespan {period_start} -> {period_end} @ {resolution} ({intervals} intervals)."

    def set_optimization_config(
        self,
        scenario_id: str,
        objective: str | None = None,
        time_limit_seconds: int | None = None,
        mip_gap: float | None = None,
        solver: str | None = None,
    ) -> str:
        """Patch the solver/objective config; only non-None fields override defaults.

        :raises ValueError: Unknown objective or solver, or invalid numeric bounds.
        """
        scenario = self.get(scenario_id)
        cfg = scenario.optimization_config
        if objective is not None:
            if objective not in _VALID_OBJECTIVES:
                raise ValueError(f"Unknown objective '{objective}'. Valid: {sorted(_VALID_OBJECTIVES)}.")
            cfg.objective = objective
        if time_limit_seconds is not None:
            if time_limit_seconds <= 0 or time_limit_seconds > 600:
                raise ValueError("time_limit_seconds must be in (0, 600].")
            cfg.time_limit_seconds = time_limit_seconds
        if mip_gap is not None:
            if mip_gap < 0 or mip_gap > 1:
                raise ValueError("mip_gap must be in [0, 1].")
            cfg.mip_gap = mip_gap
        if solver is not None:
            if solver not in _VALID_SOLVERS:
                raise ValueError(f"Unknown solver '{solver}'. Valid: {sorted(_VALID_SOLVERS)}.")
            cfg.solver = solver
        return (
            f"Optimization config: objective={cfg.objective}, time_limit={cfg.time_limit_seconds}s, "
            f"mip_gap={cfg.mip_gap}, solver={cfg.solver}."
        )

    # -------- Inspection / build -----------------------------------------

    def review(self, scenario_id: str) -> dict[str, Any]:
        """Return a structured summary of the scenario plus a validation verdict.

        The summary mirrors the eventual request shape (sites/devices/timespan/
        optimization_config) but with profiles still in their unresolved form,
        so the LLM can spot data-shape mistakes before submitting.
        """
        scenario = self.get(scenario_id)
        validation = self._validate(scenario)
        return {
            "scenario_id": scenario.id,
            "name": scenario.name,
            "description": scenario.description,
            "site_id": scenario.site_id,
            "device_count": scenario.device_count,
            "devices": [_summarize_device(d) for d in scenario.devices],
            "timespan": _summarize_timespan(scenario.timespan),
            "optimization_config": {
                "objective": scenario.optimization_config.objective,
                "time_limit_seconds": scenario.optimization_config.time_limit_seconds,
                "mip_gap": scenario.optimization_config.mip_gap,
                "solver": scenario.optimization_config.solver,
            },
            "run_count": len(scenario.runs),
            "validation": validation,
        }

    def build_request(self, scenario_id: str) -> dict[str, Any]:
        """Materialize the device-planning payload from a draft scenario.

        Resolves all profile-shaped properties to flat arrays of length
        ``timespan.intervals`` and returns the dict ready for
        :meth:`OnPremClient.device_planning`.

        :returns: Plain dict (not a pydantic model) matching the server schema.
        :raises ValueError: Scenario fails validation.
        """
        scenario = self.get(scenario_id)
        validation = self._validate(scenario)
        if not validation["valid"]:
            raise ValueError(f"Scenario {scenario_id} is not valid: {validation['errors']}")
        assert scenario.timespan is not None  # validated
        intervals = scenario.timespan.intervals

        materialized_devices: list[dict[str, Any]] = []
        for device in scenario.devices:
            schema = get_device_schema(device["type"])
            assert schema is not None  # validated
            materialized_devices.append(_materialize_device(device, schema, intervals))

        return {
            "sites": [
                {
                    "site_id": scenario.site_id,
                    "devices": materialized_devices,
                }
            ],
            "timespan": {
                "period_start": scenario.timespan.period_start,
                "period_end": scenario.timespan.period_end,
                "resolution": scenario.timespan.resolution,
            },
            "optimization_config": {
                "objective": scenario.optimization_config.objective,
                "time_limit_seconds": scenario.optimization_config.time_limit_seconds,
                "mip_gap": scenario.optimization_config.mip_gap,
                "solver": scenario.optimization_config.solver,
            },
        }

    def record_run(self, scenario_id: str, run_id: str) -> None:
        """Append a run id returned by the server to the scenario's run history."""
        scenario = self.get(scenario_id)
        scenario.runs.append(run_id)

    # -------- Validation --------------------------------------------------

    def _validate(self, scenario: Scenario) -> dict[str, Any]:
        """Return a validation dict ``{valid: bool, errors: list[str]}``."""
        errors: list[str] = []
        if scenario.timespan is None:
            errors.append("timespan not set; call set_timespan first")
            intervals: int | None = None
        else:
            try:
                intervals = scenario.timespan.intervals
            except ValueError as exc:
                errors.append(f"timespan invalid: {exc}")
                intervals = None

        if not scenario.devices:
            errors.append("no devices added; call add_device at least once")

        if intervals is not None:
            for device in scenario.devices:
                schema = get_device_schema(device["type"])
                if schema is None:
                    errors.append(f"device '{device['name']}' has unknown type '{device['type']}'")
                    continue
                for prop_name, kind in {**schema["required"], **schema["optional"]}.items():
                    if kind != "profile":
                        continue
                    if prop_name not in device["properties"]:
                        continue
                    try:
                        resolve_profile(device["properties"][prop_name], intervals)
                    except (ValueError, FileNotFoundError) as exc:
                        errors.append(f"device '{device['name']}' property '{prop_name}': {exc}")

        return {"valid": not errors, "errors": errors}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _materialize_device(device: dict[str, Any], schema: dict[str, Any], intervals: int) -> dict[str, Any]:
    """Resolve a draft device's profile properties to flat arrays."""
    props_out: dict[str, Any] = {}
    all_props = {**schema["required"], **schema["optional"]}
    for prop_name, value in device["properties"].items():
        kind = all_props.get(prop_name)
        if kind == "profile":
            props_out[prop_name] = resolve_profile(value, intervals)
        else:
            props_out[prop_name] = value
    out: dict[str, Any] = {
        "name": device["name"],
        "type": device["type"],
        "properties": props_out,
    }
    if "schedule" in device:
        out["schedule"] = device["schedule"]
    return out


def _summarize_device(device: dict[str, Any]) -> dict[str, Any]:
    """Return a compact preview of a device for review_scenario output."""
    summary: dict[str, Any] = {"name": device["name"], "type": device["type"]}
    prop_summary: dict[str, Any] = {}
    for k, v in device["properties"].items():
        if isinstance(v, list):
            prop_summary[k] = f"<array, len={len(v)}>"
        elif isinstance(v, dict):
            prop_summary[k] = f"<file ref: {v.get('file', '?')}>"
        else:
            prop_summary[k] = v
    summary["properties"] = prop_summary
    if "schedule" in device:
        summary["schedule"] = device["schedule"]
    return summary


def _summarize_timespan(ts: Timespan | None) -> dict[str, Any] | None:
    """Return a serialisable view of the timespan, or None if unset."""
    if ts is None:
        return None
    try:
        intervals = ts.intervals
    except ValueError:
        intervals = -1
    return {
        "period_start": ts.period_start,
        "period_end": ts.period_end,
        "resolution": ts.resolution,
        "intervals": intervals,
    }
