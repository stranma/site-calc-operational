"""MCP-protocol tests for the reservation-bid tools.

Each tool is exercised through ``fastmcp.Client`` against the in-process server,
so parameter binding, JSON serialisation, and tool registration are all
covered. The on-prem HTTP calls are mocked via respx.
"""

from __future__ import annotations

import base64
import json

import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError
from httpx import Response

from site_calc_operational.api.onprem_client import OnPremClient
from site_calc_operational.mcp import server as srv

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _setup_module_state() -> None:
    """Reset module-level state between tests."""
    srv._reset_store_for_tests()
    client = OnPremClient(base_url="http://stub", api_key="op_test", busy_retry=None)
    srv._set_client_for_tests(client)
    yield
    srv._set_client_for_tests(None)
    srv._reset_store_for_tests()


async def _build_binary_chp_scenario(client: Client) -> str:
    """Create a one-day scenario with a binary CHP (aFRR+/-), gas, electricity
    export, and heat export. Returns the scenario_id."""
    created = await client.call_tool("create_scenario", {"name": "rb-mcp", "site_id": "test-site"})
    scenario_id = json.loads(created.content[0].text)["scenario_id"]

    await client.call_tool(
        "set_timespan",
        {
            "scenario_id": scenario_id,
            "period_start": "2026-05-13T00:00:00+02:00",
            "period_end": "2026-05-14T00:00:00+02:00",
            "resolution": "15min",
        },
    )
    await client.call_tool(
        "add_device",
        {
            "scenario_id": scenario_id,
            "name": "CHP-bin",
            "device_type": "chp",
            "properties": {
                "gas_input": 2.5,
                "el_output": 1.0,
                "heat_output": 1.0,
                "is_binary": True,
                # ans_abilities round-trips via deepcopy in add_device; the
                # on-prem server's CHP translator reads it from properties.
                "ans_abilities": [
                    {"service": "afrr_plus", "min_device_power_rate": 0.0, "max_device_power_rate": 1.0},
                    {"service": "afrr_minus", "min_device_power_rate": 0.0, "max_device_power_rate": 1.0},
                ],
            },
        },
    )
    await client.call_tool(
        "add_device",
        {
            "scenario_id": scenario_id,
            "name": "Gas",
            "device_type": "gas_import",
            "properties": {"max_import": 2.5, "price": 45.0},
        },
    )
    await client.call_tool(
        "add_device",
        {
            "scenario_id": scenario_id,
            "name": "ElExport",
            "device_type": "electricity_export",
            "properties": {"max_export": 1.0, "price": 120.0},
        },
    )
    await client.call_tool(
        "add_device",
        {
            "scenario_id": scenario_id,
            "name": "HeatExport",
            "device_type": "heat_export",
            "properties": {"max_export": 1.0, "price": 5.0},
        },
    )
    return scenario_id


_LOGNORMAL_ACCEPTANCE = [
    {
        "service": "afrr_plus",
        "interval_start": f"2026-05-13T{h:02d}:00:00+02:00",
        "distribution": {"type": "lognormal", "mu": 1.5, "sigma": 0.6},
    }
    for h in (0, 4, 8, 12, 16, 20)
] + [
    {
        "service": "afrr_minus",
        "interval_start": f"2026-05-13T{h:02d}:00:00+02:00",
        "distribution": {"type": "lognormal", "mu": 1.0, "sigma": 0.6},
    }
    for h in (0, 4, 8, 12, 16, 20)
]


# ---------------------------------------------------------------------------
# build_reservation_bids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_build_reservation_bids_via_mcp_protocol() -> None:
    """Happy path: the MCP tool calls /v1/reservation-bids and returns the
    bundled response shape."""
    fake_response = {
        "bids": [
            {
                "service": "afrr_plus",
                "interval_start": "2026-05-13T00:00:00+02:00",
                "volume_mw": 1.0,
                "capacity_price": 25.4,
            },
        ],
        "expected_revenue": 407.46,
        "diagnostics": {"winner_is_maximal": True, "variant_count": 729},
        "most_probable_realization": {
            "contracts": [],
            "baseline_da": 12.34,
            "realized_revenue": 12.34,
            "joint_probability": 1.0,
        },
        "evaluation": {"expected_revenue": 407.46},
    }
    respx.post("http://stub/v1/reservation-bids").mock(return_value=Response(200, json=fake_response))

    async with Client(srv.mcp) as client:
        scenario_id = await _build_binary_chp_scenario(client)

        resp = await client.call_tool(
            "build_reservation_bids",
            {
                "scenario_id": scenario_id,
                "services": ["afrr_plus", "afrr_minus"],
                "acceptance": _LOGNORMAL_ACCEPTANCE,
            },
        )
        out = json.loads(resp.content[0].text)
        assert out == fake_response

    # Wire-level: payload must contain services + acceptance + sites + timespan
    sent = json.loads(respx.calls.last.request.content)
    assert sent["services"] == ["afrr_plus", "afrr_minus"]
    assert len(sent["acceptance"]) == 12
    assert sent["sites"][0]["site_id"] == "test-site"
    assert sent["timespan"]["resolution"] == "15min"


@pytest.mark.asyncio
@respx.mock
async def test_build_reservation_bids_idempotency_key_passthrough() -> None:
    respx.post("http://stub/v1/reservation-bids").mock(return_value=Response(200, json={}))

    async with Client(srv.mcp) as client:
        scenario_id = await _build_binary_chp_scenario(client)
        await client.call_tool(
            "build_reservation_bids",
            {
                "scenario_id": scenario_id,
                "services": ["afrr_plus"],
                "acceptance": _LOGNORMAL_ACCEPTANCE,
                "idempotency_key": "rb-key-mcp",
            },
        )
    assert respx.calls.last.request.headers["Idempotency-Key"] == "rb-key-mcp"


# ---------------------------------------------------------------------------
# evaluate_reservation_bids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_evaluate_reservation_bids_via_mcp_protocol() -> None:
    fake_response = {"expected_revenue": 407.461314}
    respx.post("http://stub/v1/reservation-bids/evaluate").mock(return_value=Response(200, json=fake_response))

    async with Client(srv.mcp) as client:
        scenario_id = await _build_binary_chp_scenario(client)
        bids = [
            {
                "service": "afrr_plus",
                "interval_start": "2026-05-13T16:00:00+02:00",
                "volume_mw": 1.0,
                "capacity_price": 25.4,
            },
        ]
        resp = await client.call_tool(
            "evaluate_reservation_bids",
            {"scenario_id": scenario_id, "bids": bids, "acceptance": _LOGNORMAL_ACCEPTANCE},
        )
        out = json.loads(resp.content[0].text)
    assert out == fake_response

    sent = json.loads(respx.calls.last.request.content)
    assert sent["bids"] == bids
    # Sites + timespan still carried through from the scenario.
    assert "sites" in sent and "timespan" in sent


# ---------------------------------------------------------------------------
# most_probable_realization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_most_probable_realization_via_mcp_protocol() -> None:
    fake_response = {
        "contracts": [
            {
                "service": "afrr_plus",
                "interval_start": "2026-05-13T16:00:00+02:00",
                "volume_mw": 1.0,
                "capacity_price": 25.4,
            },
        ],
        "baseline_da": 12.34,
        "realized_revenue": 419.91,
        "joint_probability": 0.083,
    }
    respx.post("http://stub/v1/reservation-bids/most-probable-realization").mock(
        return_value=Response(200, json=fake_response)
    )

    async with Client(srv.mcp) as client:
        scenario_id = await _build_binary_chp_scenario(client)
        bids = [
            {
                "service": "afrr_plus",
                "interval_start": "2026-05-13T16:00:00+02:00",
                "volume_mw": 1.0,
                "capacity_price": 25.4,
            },
        ]
        resp = await client.call_tool(
            "most_probable_realization",
            {"scenario_id": scenario_id, "bids": bids, "acceptance": _LOGNORMAL_ACCEPTANCE},
        )
        out = json.loads(resp.content[0].text)

    assert out == fake_response
    sent = json.loads(respx.calls.last.request.content)
    assert sent["bids"] == bids


# ---------------------------------------------------------------------------
# Error path: infeasible -> debug_lp materialised to disk by the tool's wrapper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_infeasible_materialises_debug_lp(tmp_path, monkeypatch) -> None:
    """Failure mode: the LP base64 blob is forwarded to the LLM as-is instead
    of being decoded and saved to disk. Verifies the existing materialisation
    helper is invoked on the reservation-bid path too."""
    monkeypatch.setenv("SITE_CALC_OPERATIONAL_DATA_DIR", str(tmp_path))

    lp_bytes = b"\\ tiny LP\nMin\nobj: x\nEnd\n"
    body = {
        "error": {
            "code": "INFEASIBLE",
            "message": "every Variant infeasible",
            "details": {
                "hint": "check budgets",
                "debug_lp_filename": "debug_problem.lp",
                "debug_lp_size_bytes": len(lp_bytes),
                "debug_lp_b64": base64.b64encode(lp_bytes).decode("ascii"),
            },
        }
    }
    respx.post("http://stub/v1/reservation-bids").mock(return_value=Response(422, json=body))

    async with Client(srv.mcp) as mcp_client:
        scenario_id = await _build_binary_chp_scenario(mcp_client)
        # fastmcp surfaces tool-side exceptions as ToolError on the protocol
        # client; the in-process server still runs the except branch, which
        # is what materialises the LP to disk before re-raising.
        with pytest.raises(ToolError, match="INFEASIBLE"):
            await mcp_client.call_tool(
                "build_reservation_bids",
                {
                    "scenario_id": scenario_id,
                    "services": ["afrr_plus", "afrr_minus"],
                    "acceptance": _LOGNORMAL_ACCEPTANCE,
                },
            )

    # The on-disk debug LP should now exist under the configured data dir.
    written = list(tmp_path.glob("debug_problem_*.lp"))
    assert len(written) == 1, f"expected one debug LP on disk, got {written}"
    assert written[0].read_bytes() == lp_bytes
