"""Shared fixtures: a small battery site, a flat day, a full forecast, and a respx-mocked client.

No test here talks to a real server; ``tests/test_production.py`` does (marker ``production``).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import respx

from site_calc_operational import (
    ANSAbility,
    AnsForecastEntry,
    Battery,
    Day,
    ElectricityExport,
    ElectricityImport,
    LogNormal,
    OperationalClient,
    PlanReservationRequest,
    Site,
)

BASE_URL = "https://operational.test"
API_KEY = "op_testkey"


@pytest.fixture
def site() -> Site:
    return Site(
        site_id="bess-1",
        devices=[
            Battery(
                name="BESS",
                capacity_mwh=2.0,
                max_power_mw=1.0,
                efficiency=0.9,
                initial_soc_mwh=1.0,
                ans_abilities=[
                    ANSAbility(service="afrr_plus", min_device_power_rate=0.0, max_device_power_rate=1.0),
                    ANSAbility(service="afrr_minus", min_device_power_rate=0.0, max_device_power_rate=1.0),
                ],
            ),
            ElectricityImport(name="GridBuy", max_import_mw=1.0, buy_fee_eur_per_mwh=1.5),
            ElectricityExport(name="GridSell", max_export_mw=1.0, sell_fee_eur_per_mwh=1.0),
        ],
    )


@pytest.fixture
def day() -> Day:
    prices = [50.0 + 30.0 * ((i // 4) % 12 >= 6) for i in range(96)]  # cheap nights, dear days
    return Day(date="2026-04-15", tz="Europe/Prague", da_price_eur_per_mwh_d=prices, da_price_eur_per_mwh_d1=prices)


@pytest.fixture
def forecast() -> list[AnsForecastEntry]:
    return [
        AnsForecastEntry(
            service=s, block_index=b, distribution=LogNormal(mu=2.0, sigma=0.5), expected_activation_eur_per_mw_h=1.0
        )
        for s in ("afrr_plus", "afrr_minus")
        for b in range(6)
    ]


@pytest.fixture
def reservation_request(site: Site, day: Day, forecast: list[AnsForecastEntry]) -> PlanReservationRequest:
    return PlanReservationRequest(site=site, day=day, services=["afrr_plus", "afrr_minus"], ans_forecast=forecast)


@pytest.fixture
def reservation_plan_body() -> dict[str, Any]:
    bid = {
        "service": "afrr_plus",
        "block_index": 2,
        "interval_start": "2026-04-15T08:00:00+02:00",
        "volume_mw": 1.0,
        "capacity_price_eur_per_mw_h": 7.5,
    }
    return {
        "bids": [{**bid, "forecast_clear_probability": 0.62}],
        "expected_revenue": {"reservation": 30.0, "activation": 4.0, "da_arbitrage": 20.0, "total": 54.0},
        "most_probable_realization": {
            "contracts": [bid],
            "baseline_da": 22.0,
            "realized_revenue": 52.0,
            "joint_probability": 0.4,
        },
        "diagnostics": {"winner_is_maximal": True},
        "run": {
            "site_calc_version": "1.5.0",
            "site_calc_commit_sha": "abc123",
            "planner": "sitecalc",
            "params": {},
            "horizon_qh": 192,
            "solve_seconds": 12.5,
        },
    }


@pytest.fixture
def day_ahead_plan_body() -> dict[str, Any]:
    return {
        "bids": [
            {
                "qh_index": i,
                "interval_start": f"2026-04-15T{i // 4:02d}:{15 * (i % 4):02d}:00+02:00",
                "volume_mw": 0.5 if i >= 48 else -0.5,
                "price_eur_per_mwh": -500.0 if i >= 48 else 4000.0,
            }
            for i in range(96)
        ],
        "schedule": {
            "net_flow_mw": [0.0] * 192,
            "committed_qh": 96,
            "soc_mwh": [1.0] * 192,
            "soc_min_mwh": [0.0] * 192,
            "soc_max_mwh": [2.0] * 192,
            "band_lo_mw": [-1.0] * 192,
            "band_hi_mw": [1.0] * 192,
        },
        "soc_end_mwh": 0.75,
        "objective_eur": 41.0,
        "expected_da_value_eur": 44.0,
        "market_fees_eur": -3.0,
        "run": {
            "site_calc_version": "1.5.0",
            "site_calc_commit_sha": "abc123",
            "planner": "day-ahead",
            "params": {},
            "horizon_qh": 192,
            "solve_seconds": 0.3,
        },
    }


@pytest.fixture
def mock_api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        yield router


@pytest.fixture
def client(mock_api: respx.MockRouter) -> Iterator[OperationalClient]:
    with OperationalClient(BASE_URL, API_KEY, busy_retry=None) as c:
        yield c
