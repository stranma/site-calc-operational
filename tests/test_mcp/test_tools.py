"""Tests for individual MCP tool functions (with the SDK mocked at the HTTP layer)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import respx
from httpx import Response

from site_calc_operational.api.onprem_client import OnPremClient
from site_calc_operational.api.onprem_exceptions import NotImplementedOnServer
from site_calc_operational.mcp import server as srv

# ---------------------------------------------------------------------------
# Fixture: install a respx-driven OnPremClient as the module-level singleton
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_module_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test gets a fresh module store and an injected client.

    Without this fixture, tools accumulate state between tests (the store is a
    process-wide singleton) and leak the OnPremClient created from a previous
    test's env vars.
    """
    srv._reset_store_for_tests()
    client = OnPremClient(base_url="http://stub", api_key="op_test", busy_retry=None)
    srv._set_client_for_tests(client)
    yield
    srv._set_client_for_tests(None)
    srv._reset_store_for_tests()


# ---------------------------------------------------------------------------
# Server-info tools
# ---------------------------------------------------------------------------


@respx.mock
def test_health_tool_parses_server_response() -> None:
    """Failure mode: health() returns an opaque object instead of plain JSON,
    causing FastMCP to drop fields when serialising the tool response."""
    respx.get("http://stub/v1/health").mock(
        return_value=Response(
            200,
            json={
                "status": "ok",
                "site_calc_version": "1.5.2",
                "site_calc_commit_sha": "abc",
                "service_version": "0.1.0",
                "db_ok": True,
                "active_solve": False,
            },
        )
    )
    result = srv.health()
    assert result["status"] == "ok"
    assert result["db_ok"] is True
    assert result["active_solve"] is False


@respx.mock
def test_get_version_includes_server_when_reachable() -> None:
    """Failure mode: get_version reports stale or hardcoded server version,
    so compatibility checks pass even when the wire protocol drifts."""
    respx.get("http://stub/v1/health").mock(
        return_value=Response(
            200,
            json={
                "status": "ok",
                "site_calc_version": "1.5.2",
                "site_calc_commit_sha": "abc1234567",
                "service_version": "0.1.0",
                "db_ok": True,
                "active_solve": False,
            },
        )
    )
    result = srv.get_version()
    assert "client_version" in result
    assert result["server_status"] == "ok"
    assert result["server_commit_sha"] == "abc1234567"
    assert result["server_site_calc_version"] == "1.5.2"


@respx.mock
def test_get_version_handles_unreachable_server() -> None:
    """Failure mode: an unreachable server propagates a raw exception, making
    the LLM error path noisy rather than informative."""
    respx.get("http://stub/v1/health").mock(return_value=Response(503))
    result = srv.get_version()
    assert "client_version" in result
    assert "error" in result


# ---------------------------------------------------------------------------
# Scenario assembly
# ---------------------------------------------------------------------------


def test_create_scenario_returns_id_and_name() -> None:
    """Failure mode: create_scenario silently drops site_id, so subsequent
    build_request emits an empty site identifier."""
    out = srv.create_scenario(name="Plant A 24h", site_id="plant-a")
    assert "scenario_id" in out
    assert out["site_id"] == "plant-a"


def test_add_remove_device_round_trip() -> None:
    """Failure mode: add_device and remove_device are not symmetric -- after
    removing a device its name remains 'taken' so re-adding raises."""
    sid = srv.create_scenario(name="X", site_id="s")["scenario_id"]
    srv.set_timespan(sid, "2026-01-15T00:00:00+00:00", "2026-01-16T00:00:00+00:00", "1h")
    srv.add_device(sid, "Bat", "battery", {"capacity": 1.0, "max_power": 1.0, "efficiency": 0.9})
    srv.remove_device(sid, "Bat")
    # Re-add must succeed.
    srv.add_device(sid, "Bat", "battery", {"capacity": 2.0, "max_power": 2.0, "efficiency": 0.9})
    review = srv.review_scenario(sid)
    assert review["device_count"] == 1
    assert review["devices"][0]["properties"]["capacity"] == 2.0


def test_set_optimization_config_round_trip() -> None:
    """Failure mode: optimization_config defaults are silently overwritten on
    every call instead of being patched, so partial updates lose other fields."""
    sid = srv.create_scenario(name="X", site_id="s")["scenario_id"]
    srv.set_optimization_config(sid, time_limit_seconds=60)
    srv.set_optimization_config(sid, mip_gap=0.005)
    review = srv.review_scenario(sid)
    cfg = review["optimization_config"]
    assert cfg["time_limit_seconds"] == 60
    assert cfg["mip_gap"] == 0.005
    assert cfg["objective"] == "maximize_profit"  # default preserved


def test_list_scenarios_reflects_store_state() -> None:
    """Failure mode: list_scenarios reports stale rows after a delete, so the
    LLM keeps trying to operate on a missing scenario."""
    a = srv.create_scenario(name="A", site_id="s1")["scenario_id"]
    b = srv.create_scenario(name="B", site_id="s2")["scenario_id"]
    srv.delete_scenario(a)
    summaries = srv.list_scenarios()
    assert {s["scenario_id"] for s in summaries} == {b}


def test_delete_scenario_idempotent_for_unknown_id() -> None:
    """Failure mode: delete_scenario raises on unknown ids, breaking cleanup
    after the LLM has already lost track of which ids exist."""
    out = srv.delete_scenario("never-existed")
    assert "Deleted" in out


# ---------------------------------------------------------------------------
# Solve
# ---------------------------------------------------------------------------


def _build_solveable_scenario() -> str:
    sid = srv.create_scenario(name="solveable", site_id="site")["scenario_id"]
    srv.set_timespan(sid, "2026-01-15T00:00:00+00:00", "2026-01-16T00:00:00+00:00", "1h")
    srv.add_device(sid, "Bat", "battery", {"capacity": 6.0, "max_power": 3.0, "efficiency": 0.9})
    srv.add_device(sid, "Imp", "electricity_import", {"max_import": 10.0, "price": 50.0})
    srv.add_device(sid, "HD", "heat_demand", {"demand_profile": [2.0] * 24})
    return sid


@respx.mock
def test_solve_records_run_and_returns_summary(healthy_chp_battery_payload: dict) -> None:
    """Failure mode: solve() returns the raw response dict but doesn't extract
    the summary or record the run_id, so list_scenarios undercounts runs."""
    respx.post("http://stub/v1/device-planning").mock(return_value=Response(200, json=healthy_chp_battery_payload))

    sid = _build_solveable_scenario()
    out = srv.solve(sid)
    assert out["run_id"] == healthy_chp_battery_payload["run_id"]
    assert out["solver_status"] == "Optimal"
    assert "expected_profit" in out["summary"]

    # The run_id must be recorded on the scenario.
    listed = srv.list_scenarios()
    matches = [s for s in listed if s["scenario_id"] == sid]
    assert matches and matches[0]["run_count"] == 1


@respx.mock
def test_solve_propagates_validation_errors_locally() -> None:
    """Failure mode: solve() submits an invalid scenario to the server,
    burning the solver slot and producing a 422 instead of a local error."""
    sid = srv.create_scenario(name="empty", site_id="s")["scenario_id"]
    # No devices, no timespan -> validation must fail before any HTTP call.
    route = respx.post("http://stub/v1/device-planning").mock(return_value=Response(200, json={}))
    with pytest.raises(ValueError):
        srv.solve(sid)
    assert route.called is False


@respx.mock
def test_solve_passes_idempotency_key_header(healthy_chp_battery_payload: dict) -> None:
    """Failure mode: idempotency_key is silently dropped, so replays cost a
    full solve instead of returning the cached response."""
    route = respx.post("http://stub/v1/device-planning").mock(
        return_value=Response(200, json=healthy_chp_battery_payload)
    )

    sid = _build_solveable_scenario()
    srv.solve(sid, idempotency_key="abc-123")
    assert route.called
    sent_headers = route.calls[0].request.headers
    assert sent_headers.get("Idempotency-Key") == "abc-123"


# ---------------------------------------------------------------------------
# Run inspection
# ---------------------------------------------------------------------------


@respx.mock
def test_get_run_passes_through_response() -> None:
    """Failure mode: get_run returns a half-decoded object that the MCP layer
    can't serialise, so the LLM sees an opaque error instead of the run dict."""
    expected = {"id": "11111111-1111-1111-1111-111111111111", "status": "ok", "duration_ms": 50}
    respx.get(f"http://stub/v1/runs/{expected['id']}").mock(return_value=Response(200, json=expected))
    assert srv.get_run(expected["id"]) == expected


@respx.mock
def test_list_runs_forwards_filters() -> None:
    """Failure mode: filter args are dropped on the wire, so the LLM gets all
    runs back when it asks for ones matching a specific endpoint+status."""
    route = respx.get("http://stub/v1/runs").mock(return_value=Response(200, json={"runs": [], "next_before": None}))
    srv.list_runs(endpoint="device-planning", status="ok", limit=10, before="2026-05-01T00:00:00Z")
    assert route.called
    qs = route.calls[0].request.url.params
    assert qs["endpoint"] == "device-planning"
    assert qs["status"] == "ok"
    assert qs["limit"] == "10"
    assert qs["before"] == "2026-05-01T00:00:00Z"


@respx.mock
def test_cancel_active_distinguishes_idle_vs_cancelled() -> None:
    """Failure mode: cancel_active returns the same shape on 200 and 204, so the
    LLM cannot tell whether anything was actually cancelled."""
    respx.post("http://stub/v1/runs/active/cancel").mock(return_value=Response(204))
    idle = srv.cancel_active()
    assert idle == {"cancelled": False, "message": "No solve was active."}

    respx.post("http://stub/v1/runs/active/cancel").mock(
        return_value=Response(200, json={"id": "rid", "status": "cancelled"})
    )
    cancelled = srv.cancel_active()
    assert cancelled["cancelled"] is True
    assert cancelled["run"]["id"] == "rid"


# ---------------------------------------------------------------------------
# Schema helper
# ---------------------------------------------------------------------------


def test_get_device_schema_returns_required_kinds() -> None:
    """Failure mode: schema description omits the kind tag (scalar/profile/object),
    so the LLM cannot tell which fields accept arrays."""
    schema = srv.get_device_schema("battery")
    assert schema["device_type"] == "battery"
    assert schema["required"]["capacity"] == "scalar"
    assert schema["supports_schedule"] is True


def test_get_device_schema_unknown_returns_error() -> None:
    """Failure mode: unknown device types raise instead of returning a
    structured error, so add_device's preflight branch breaks."""
    schema = srv.get_device_schema("nope")
    assert "error" in schema
    assert "valid_types" in schema
    assert "battery" in schema["valid_types"]


# ---------------------------------------------------------------------------
# Data file tools
# ---------------------------------------------------------------------------


def test_save_data_file_writes_and_reports_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Failure mode: save_data_file reports the wrong row count or wrong path,
    so subsequent {"file": "..."} references go to a nonexistent file."""
    monkeypatch.setenv("SITE_CALC_OPERATIONAL_DATA_DIR", str(tmp_path))
    out = srv.save_data_file("prices.csv", {"hour": [0.0, 1.0], "price": [50.0, 60.0]}, overwrite=False)
    assert out["rows"] == 2
    assert out["columns"] == ["hour", "price"]
    assert os.path.exists(out["file_path"])


@respx.mock
def test_fetch_url_uses_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Failure mode: SITE_CALC_OPERATIONAL_DATA_DIR is ignored, so downloads
    land in cwd and the LLM can't relocate them."""
    monkeypatch.setenv("SITE_CALC_OPERATIONAL_DATA_DIR", str(tmp_path))
    body = "h\n1\n2\n"
    respx.get("http://example.test/d.csv").mock(return_value=Response(200, text=body))
    out = srv.fetch_url("http://example.test/d.csv")
    assert os.path.normcase(out["file_path"]).startswith(os.path.normcase(str(tmp_path)))


# ---------------------------------------------------------------------------
# Optimal-bidding propagation -- documents the expected 501 surface
# ---------------------------------------------------------------------------


@respx.mock
def test_solve_propagates_busy_error_when_server_returns_503() -> None:
    """Failure mode: 503 is silently swallowed and the LLM thinks the solve
    succeeded with empty results."""
    from site_calc_operational.api.onprem_exceptions import BusyError

    respx.post("http://stub/v1/device-planning").mock(
        return_value=Response(
            503,
            json={"error": {"code": "BUSY", "message": "another solve in progress"}},
        )
    )
    sid = _build_solveable_scenario()
    with pytest.raises(BusyError):
        srv.solve(sid)


@respx.mock
def test_solve_propagates_validation_error_from_server() -> None:
    """Failure mode: 422 is converted to a generic Exception with the wrong
    type, so caller code can't catch ValidationError specifically."""
    from site_calc_operational.api.onprem_exceptions import ValidationError

    respx.post("http://stub/v1/device-planning").mock(
        return_value=Response(
            422,
            json={"error": {"code": "INVALID_REQUEST", "message": "bad payload"}},
        )
    )
    sid = _build_solveable_scenario()
    with pytest.raises(ValidationError):
        srv.solve(sid)


# Clean up unused import warning for a rarely-imported exception.
_ = NotImplementedOnServer
