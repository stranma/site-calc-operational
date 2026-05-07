"""Shared fixtures for operational MCP tests."""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path

import pytest

from site_calc_operational.mcp.scenario import OperationalScenarioStore


@pytest.fixture
def store() -> OperationalScenarioStore:
    """Fresh store per test."""
    return OperationalScenarioStore()


@pytest.fixture
def scenario_id(store: OperationalScenarioStore) -> str:
    """Pre-created scenario with timespan set (24 hourly intervals)."""
    sid = store.create(name="Test Scenario", site_id="test-site", description="for tests")
    store.set_timespan(
        sid,
        period_start="2026-01-15T00:00:00+00:00",
        period_end="2026-01-16T00:00:00+00:00",
        resolution="1h",
    )
    return sid


@pytest.fixture
def tmp_csv(tmp_path: Path) -> Generator[str, None, None]:
    """A 24-row CSV with hour and price_eur_mwh columns (matches scenario_id timespan)."""
    path = tmp_path / "prices.csv"
    with open(path, "w", newline="") as f:
        f.write("hour,price_eur_mwh\n")
        for i in range(24):
            f.write(f"{i},{50.0 + (i % 12)}\n")
    yield str(path)


@pytest.fixture
def tmp_json(tmp_path: Path) -> Generator[str, None, None]:
    """A 24-element flat JSON array (matches scenario_id timespan)."""
    path = tmp_path / "demand.json"
    with open(path, "w") as f:
        json.dump([2.0 + (i % 5) * 0.5 for i in range(24)], f)
    yield str(path)


@pytest.fixture
def tmp_csv_no_header(tmp_path: Path) -> Generator[str, None, None]:
    """A 24-row CSV without a header row."""
    path = tmp_path / "no_header.csv"
    with open(path, "w", newline="") as f:
        for i in range(24):
            f.write(f"{50.0 + (i % 12)}\n")
    yield str(path)


@pytest.fixture
def healthy_chp_battery_payload() -> dict[str, object]:
    """A sample server-side response shape for /v1/device-planning success."""
    return {
        "run_id": "11111111-1111-1111-1111-111111111111",
        "summary": {
            "expected_profit": 482.0,
            "solver_status": "Optimal",
            "solve_time_seconds": 0.5,
        },
        "sites": {},
    }
