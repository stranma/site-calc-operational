"""End-to-end integration test using fastmcp.Client against the in-process FastMCP server.

This exercises the full MCP protocol path: tool registration, parameter binding,
JSON serialisation/deserialisation. The on-prem HTTP calls are mocked via respx
so the tests are fully offline.
"""

from __future__ import annotations

import json

import pytest
import respx
from fastmcp import Client
from httpx import Response

from site_calc_operational.api.onprem_client import OnPremClient
from site_calc_operational.mcp import server as srv

EXPECTED_TOOL_COUNT = 17


@pytest.fixture(autouse=True)
def _setup_module_state() -> None:
    """Reset module-level state between tests."""
    srv._reset_store_for_tests()
    client = OnPremClient(base_url="http://stub", api_key="op_test", busy_retry=None)
    srv._set_client_for_tests(client)
    yield
    srv._set_client_for_tests(None)
    srv._reset_store_for_tests()


@pytest.mark.asyncio
async def test_lists_expected_tool_count() -> None:
    """Failure mode: a tool is added to scenarios.py but not registered with
    mcp.tool(), or vice versa -- the registered count drifts from documentation."""
    async with Client(srv.mcp) as client:
        tools = await client.list_tools()
    assert len(tools) == EXPECTED_TOOL_COUNT, (
        f"Expected {EXPECTED_TOOL_COUNT} MCP tools, got {len(tools)}: {[t.name for t in tools]}"
    )


@pytest.mark.asyncio
async def test_tool_names_are_present() -> None:
    """Failure mode: a tool gets renamed (e.g. solve -> run_solve) without
    updating callers/documentation."""
    async with Client(srv.mcp) as client:
        tools = await client.list_tools()
    names = {t.name for t in tools}
    expected = {
        "get_version",
        "health",
        "create_scenario",
        "add_device",
        "remove_device",
        "set_timespan",
        "set_optimization_config",
        "review_scenario",
        "delete_scenario",
        "list_scenarios",
        "solve",
        "get_run",
        "list_runs",
        "cancel_active",
        "get_device_schema",
        "save_data_file",
        "fetch_url",
    }
    assert names == expected, f"Tool name drift: missing={expected - names}, extra={names - expected}"


@pytest.mark.asyncio
@respx.mock
async def test_full_workflow_via_mcp_protocol(healthy_chp_battery_payload: dict) -> None:
    """Failure mode: the JSON parameter binding loses dict-shaped properties,
    so add_device receives an empty properties dict and validation fails."""
    respx.post("http://stub/v1/device-planning").mock(return_value=Response(200, json=healthy_chp_battery_payload))

    async with Client(srv.mcp) as client:
        created = await client.call_tool("create_scenario", {"name": "Smoke", "site_id": "site-1"})
        scenario_id = json.loads(created.content[0].text)["scenario_id"]

        await client.call_tool(
            "set_timespan",
            {
                "scenario_id": scenario_id,
                "period_start": "2026-01-15T00:00:00+00:00",
                "period_end": "2026-01-16T00:00:00+00:00",
                "resolution": "1h",
            },
        )
        await client.call_tool(
            "add_device",
            {
                "scenario_id": scenario_id,
                "name": "Bat",
                "device_type": "battery",
                "properties": {"capacity": 6.0, "max_power": 3.0, "efficiency": 0.9},
            },
        )
        await client.call_tool(
            "add_device",
            {
                "scenario_id": scenario_id,
                "name": "Imp",
                "device_type": "electricity_import",
                "properties": {"max_import": 10.0, "price": 50.0},
            },
        )
        await client.call_tool(
            "add_device",
            {
                "scenario_id": scenario_id,
                "name": "HD",
                "device_type": "heat_demand",
                "properties": {"demand_profile": [2.0] * 24},
            },
        )

        review_resp = await client.call_tool("review_scenario", {"scenario_id": scenario_id})
        review = json.loads(review_resp.content[0].text)
        assert review["validation"]["valid"] is True

        solve_resp = await client.call_tool("solve", {"scenario_id": scenario_id})
        solve_out = json.loads(solve_resp.content[0].text)
        assert solve_out["run_id"] == healthy_chp_battery_payload["run_id"]
        assert solve_out["solver_status"] == "Optimal"


@pytest.mark.asyncio
async def test_review_validation_surfaces_missing_pieces_via_protocol() -> None:
    """Failure mode: the review tool emits validation errors as a list of objects
    instead of strings, so MCP clients can't render them."""
    async with Client(srv.mcp) as client:
        created = await client.call_tool("create_scenario", {"name": "Empty", "site_id": "s"})
        scenario_id = json.loads(created.content[0].text)["scenario_id"]
        review = await client.call_tool("review_scenario", {"scenario_id": scenario_id})

    review_data = json.loads(review.content[0].text)
    assert review_data["validation"]["valid"] is False
    assert all(isinstance(err, str) for err in review_data["validation"]["errors"])
