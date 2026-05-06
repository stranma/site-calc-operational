"""Tests for OperationalScenarioStore: CRUD, validation, build_request."""

from __future__ import annotations

import pytest

from site_calc_operational.mcp.scenario import (
    OperationalScenarioStore,
    Scenario,
    Timespan,
    get_device_schema,
    known_device_types,
)

# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_create_returns_unique_ids(store: OperationalScenarioStore) -> None:
    """Failure mode: create() reuses ids, so the second scenario silently
    overwrites the first."""
    a = store.create(name="A", site_id="site-a")
    b = store.create(name="B", site_id="site-b")
    assert a != b
    assert {a, b} == {s.id for s in store.list()}
    assert store.get(a).name == "A"
    assert store.get(b).name == "B"


def test_create_rejects_blank_name(store: OperationalScenarioStore) -> None:
    """Failure mode: blank scenario name slips through, leading to confusing
    summaries downstream."""
    with pytest.raises(ValueError, match="non-empty"):
        store.create(name="   ", site_id="site")
    with pytest.raises(ValueError, match="non-empty"):
        store.create(name="OK", site_id=" ")


def test_get_unknown_id_raises(store: OperationalScenarioStore) -> None:
    """Failure mode: get() returns None on unknown id, callers operate on a
    placeholder, and the error surface much later."""
    with pytest.raises(KeyError, match="Unknown scenario_id"):
        store.get("nope")


def test_delete_is_idempotent(store: OperationalScenarioStore) -> None:
    """Failure mode: delete on an unknown id raises and breaks the cleanup
    sequence in error-recovery code paths."""
    store.delete("never-existed")  # no raise
    sid = store.create(name="A", site_id="site")
    store.delete(sid)
    assert store.list() == []


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


def test_add_device_appends_and_validates_required(scenario_id: str, store: OperationalScenarioStore) -> None:
    """Failure mode: a battery without 'efficiency' is silently accepted, so the
    server returns a 422 instead of the LLM seeing the issue locally."""
    store.add_device(
        scenario_id,
        name="Bat",
        device_type="battery",
        properties={"capacity": 6.0, "max_power": 3.0, "efficiency": 0.9},
    )
    with pytest.raises(ValueError, match="Missing required properties"):
        store.add_device(
            scenario_id,
            name="Bat2",
            device_type="battery",
            properties={"capacity": 6.0, "max_power": 3.0},  # missing efficiency
        )
    sc = store.get(scenario_id)
    assert sc.device_count == 1


def test_add_device_rejects_duplicate_names(scenario_id: str, store: OperationalScenarioStore) -> None:
    """Failure mode: two devices share a name, so server-side de-duplication
    overwrites one silently."""
    store.add_device(
        scenario_id,
        name="Bat",
        device_type="battery",
        properties={"capacity": 6.0, "max_power": 3.0, "efficiency": 0.9},
    )
    with pytest.raises(ValueError, match="already exists"):
        store.add_device(
            scenario_id,
            name="Bat",
            device_type="battery",
            properties={"capacity": 1.0, "max_power": 1.0, "efficiency": 0.9},
        )


def test_add_device_rejects_unknown_type(scenario_id: str, store: OperationalScenarioStore) -> None:
    """Failure mode: typo'd device type passes through and the server returns
    a 422 the LLM can't easily diagnose."""
    with pytest.raises(ValueError, match="Unknown device type"):
        store.add_device(
            scenario_id,
            name="X",
            device_type="batter",  # typo
            properties={"capacity": 1.0},
        )


def test_add_device_rejects_schedule_for_unsupported(scenario_id: str, store: OperationalScenarioStore) -> None:
    """Failure mode: schedule is attached to a device type that ignores it,
    leading to confusing solver behaviour."""
    with pytest.raises(ValueError, match="does not support schedule"):
        store.add_device(
            scenario_id,
            name="Imp",
            device_type="electricity_import",
            properties={"max_import": 10.0, "price": 50.0},
            schedule={"can_run": [True] * 24},
        )


def test_remove_device_round_trip(scenario_id: str, store: OperationalScenarioStore) -> None:
    """Failure mode: remove_device fails silently for unknown names, hiding
    typos when the LLM tries to revise the scenario."""
    store.add_device(
        scenario_id,
        name="Bat",
        device_type="battery",
        properties={"capacity": 6.0, "max_power": 3.0, "efficiency": 0.9},
    )
    store.remove_device(scenario_id, "Bat")
    assert store.get(scenario_id).device_count == 0
    with pytest.raises(ValueError, match="not found"):
        store.remove_device(scenario_id, "Bat")


# ---------------------------------------------------------------------------
# Timespan
# ---------------------------------------------------------------------------


def test_set_timespan_computes_intervals_correctly(store: OperationalScenarioStore) -> None:
    """Failure mode: interval count is off by one, so profile-array length
    validation produces false positives or negatives."""
    sid = store.create(name="A", site_id="s1")
    store.set_timespan(sid, "2026-01-15T00:00:00+00:00", "2026-01-16T00:00:00+00:00", "1h")
    assert store.get(sid).timespan is not None
    assert store.get(sid).timespan.intervals == 24

    sid2 = store.create(name="B", site_id="s2")
    store.set_timespan(sid2, "2026-01-15T00:00:00+00:00", "2026-01-15T06:00:00+00:00", "15min")
    assert store.get(sid2).timespan.intervals == 24


def test_set_timespan_rejects_non_whole_horizon(scenario_id: str, store: OperationalScenarioStore) -> None:
    """Failure mode: 90 min horizon at 1h resolution is silently rounded,
    producing length-mismatched profile arrays."""
    with pytest.raises(ValueError, match="not a whole multiple"):
        store.set_timespan(scenario_id, "2026-01-15T00:00:00+00:00", "2026-01-15T01:30:00+00:00", "1h")


def test_set_timespan_rejects_inverted_window(scenario_id: str, store: OperationalScenarioStore) -> None:
    """Failure mode: end before start produces a negative-length horizon that
    only fails much later inside the solver."""
    with pytest.raises(ValueError, match="must be after"):
        store.set_timespan(scenario_id, "2026-01-16T00:00:00+00:00", "2026-01-15T00:00:00+00:00", "1h")


def test_set_timespan_rejects_unknown_resolution(scenario_id: str, store: OperationalScenarioStore) -> None:
    """Failure mode: resolution typo (5min) is forwarded to the server, which
    returns a 422 instead of the LLM seeing it locally."""
    with pytest.raises(ValueError, match="Unknown resolution"):
        store.set_timespan(scenario_id, "2026-01-15T00:00:00+00:00", "2026-01-16T00:00:00+00:00", "5min")


# ---------------------------------------------------------------------------
# Optimization config
# ---------------------------------------------------------------------------


def test_set_optimization_config_partial_update(scenario_id: str, store: OperationalScenarioStore) -> None:
    """Failure mode: passing only objective resets the other fields to defaults,
    losing previously-set time_limit / mip_gap / solver."""
    store.set_optimization_config(scenario_id, time_limit_seconds=300, mip_gap=0.001, solver="cbc")
    store.set_optimization_config(scenario_id, objective="minimize_cost")
    cfg = store.get(scenario_id).optimization_config
    assert cfg.objective == "minimize_cost"
    assert cfg.time_limit_seconds == 300
    assert cfg.mip_gap == 0.001
    assert cfg.solver == "cbc"


def test_set_optimization_config_rejects_bad_values(scenario_id: str, store: OperationalScenarioStore) -> None:
    """Failure mode: invalid mip_gap / time_limit / solver / objective slip
    through to the server, which returns 422 with less context."""
    with pytest.raises(ValueError, match="Unknown objective"):
        store.set_optimization_config(scenario_id, objective="maximise_profit")  # British spelling
    with pytest.raises(ValueError, match="time_limit_seconds"):
        store.set_optimization_config(scenario_id, time_limit_seconds=0)
    with pytest.raises(ValueError, match="time_limit_seconds"):
        store.set_optimization_config(scenario_id, time_limit_seconds=601)
    with pytest.raises(ValueError, match="mip_gap"):
        store.set_optimization_config(scenario_id, mip_gap=1.5)
    with pytest.raises(ValueError, match="Unknown solver"):
        store.set_optimization_config(scenario_id, solver="lpsolve")


# ---------------------------------------------------------------------------
# Validation + build_request
# ---------------------------------------------------------------------------


def test_review_flags_missing_devices_and_timespan(store: OperationalScenarioStore) -> None:
    """Failure mode: review reports validation.valid=True for empty scenarios,
    so the LLM submits an empty payload and only sees the error from the server."""
    sid = store.create(name="empty", site_id="s")
    review = store.review(sid)
    assert review["validation"]["valid"] is False
    errs = " ".join(review["validation"]["errors"])
    assert "timespan" in errs
    assert "devices" in errs


def test_review_flags_profile_length_mismatch(scenario_id: str, store: OperationalScenarioStore) -> None:
    """Failure mode: a 12-value price array on a 24-interval scenario is
    accepted; the server rejects only at solve time."""
    store.add_device(
        scenario_id,
        name="Imp",
        device_type="electricity_import",
        properties={"max_import": 10.0, "price": [50.0] * 12},  # too short
    )
    review = store.review(scenario_id)
    assert review["validation"]["valid"] is False
    assert any("does not match expected length" in err for err in review["validation"]["errors"])


def test_build_request_resolves_scalars_and_arrays(scenario_id: str, store: OperationalScenarioStore) -> None:
    """Failure mode: build_request leaves scalars unbroadcast, sending the
    server invalid request bodies."""
    store.add_device(
        scenario_id,
        name="Bat",
        device_type="battery",
        properties={"capacity": 6.0, "max_power": 3.0, "efficiency": 0.9, "initial_soc": 0.5},
    )
    store.add_device(
        scenario_id,
        name="Imp",
        device_type="electricity_import",
        properties={"max_import": 10.0, "price": 50.0},  # scalar -> 24 element array
    )
    store.add_device(
        scenario_id,
        name="Exp",
        device_type="electricity_export",
        properties={"max_export": 10.0, "price": [40.0 + i for i in range(24)]},  # explicit
    )
    payload = store.build_request(scenario_id)
    assert len(payload["sites"]) == 1
    devices = payload["sites"][0]["devices"]
    imp = next(d for d in devices if d["name"] == "Imp")
    assert imp["properties"]["price"] == [50.0] * 24
    exp = next(d for d in devices if d["name"] == "Exp")
    assert exp["properties"]["price"][0] == 40.0
    assert exp["properties"]["price"][23] == 63.0
    bat = next(d for d in devices if d["name"] == "Bat")
    # Scalars on non-profile properties are passed through unchanged.
    assert bat["properties"]["capacity"] == 6.0
    assert bat["properties"]["initial_soc"] == 0.5


def test_build_request_resolves_csv_and_json_files(
    scenario_id: str,
    store: OperationalScenarioStore,
    tmp_csv: str,
    tmp_json: str,
) -> None:
    """Failure mode: file references are forwarded raw to the server, which
    returns 422 because it cannot read the user's local filesystem."""
    store.add_device(
        scenario_id,
        name="Imp",
        device_type="electricity_import",
        properties={"max_import": 10.0, "price": {"file": tmp_csv, "column": "price_eur_mwh"}},
    )
    store.add_device(
        scenario_id,
        name="HD",
        device_type="heat_demand",
        properties={"demand_profile": {"file": tmp_json}},
    )
    payload = store.build_request(scenario_id)
    devices = payload["sites"][0]["devices"]
    imp = next(d for d in devices if d["name"] == "Imp")
    assert isinstance(imp["properties"]["price"], list)
    assert len(imp["properties"]["price"]) == 24
    hd = next(d for d in devices if d["name"] == "HD")
    assert len(hd["properties"]["demand_profile"]) == 24


def test_build_request_refuses_invalid_scenario(scenario_id: str, store: OperationalScenarioStore) -> None:
    """Failure mode: build_request silently materializes invalid payloads,
    so the LLM doesn't get a precise local error and burns a server slot."""
    # No devices -> not valid
    with pytest.raises(ValueError, match="not valid"):
        store.build_request(scenario_id)


def test_record_run_appends_unique_history(scenario_id: str, store: OperationalScenarioStore) -> None:
    """Failure mode: record_run drops existing run ids, breaking history."""
    store.record_run(scenario_id, "run-a")
    store.record_run(scenario_id, "run-b")
    sc = store.get(scenario_id)
    assert sc.runs == ["run-a", "run-b"]


# ---------------------------------------------------------------------------
# Schema introspection
# ---------------------------------------------------------------------------


def test_known_device_types_includes_core_types() -> None:
    """Failure mode: a device type is dropped from the registry without test
    coverage, so the LLM cannot add it via the MCP tool."""
    types = known_device_types()
    for required in {"battery", "chp", "heat_accumulator", "electricity_import", "electricity_export", "heat_demand"}:
        assert required in types, f"missing device type: {required}"


def test_get_device_schema_returns_none_for_unknown() -> None:
    """Failure mode: get_device_schema returns a stub schema for unknown types,
    so add_device's downstream validation accepts garbage."""
    assert get_device_schema("unicorn") is None


def test_get_device_schema_marks_profile_kinds() -> None:
    """Failure mode: a 'price' field is marked as scalar instead of profile,
    so it is not broadcast/length-validated and the server rejects the payload."""
    schema = get_device_schema("electricity_import")
    assert schema is not None
    assert schema["required"]["price"] == "profile"
    assert schema["required"]["max_import"] == "scalar"


# ---------------------------------------------------------------------------
# Dataclass smoke tests
# ---------------------------------------------------------------------------


def test_scenario_dataclass_defaults() -> None:
    """Failure mode: Scenario default factories are shared between instances,
    so adding a device to one scenario mutates another."""
    a = Scenario(id="a", name="a", site_id="s")
    b = Scenario(id="b", name="b", site_id="s")
    a.devices.append({"name": "x", "type": "battery", "properties": {}})
    assert b.devices == []  # not the same list object
    assert a.optimization_config is not b.optimization_config


def test_timespan_intervals_reports_consistent_count() -> None:
    """Failure mode: Timespan.intervals depends on string parsing locale, so
    the same datetime returns different counts on different machines."""
    ts = Timespan(period_start="2026-05-01T00:00:00+00:00", period_end="2026-05-08T00:00:00+00:00", resolution="1h")
    assert ts.intervals == 7 * 24
