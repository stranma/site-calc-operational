"""Against a live server (marker ``production``; excluded by default).

    SITE_CALC_OPERATIONAL_URL=https://... SITE_CALC_OPERATIONAL_API_KEY=op_... \
        uv run pytest -m production

Plans a small synthetic battery day twice: the reservation step with the
fast ``baseline`` planner and the day-ahead step with the most probable
contracts cleared. Two runs land in the server's ``runs`` table.
"""

from __future__ import annotations

import os
import uuid

import pytest

from site_calc_operational import (
    ClearedReservation,
    DayNotPlannableError,
    OperationalClient,
    PlanDayAheadRequest,
    PlanReservationRequest,
    ReservationParams,
)

pytestmark = pytest.mark.production

URL = os.environ.get("SITE_CALC_OPERATIONAL_URL")
KEY = os.environ.get("SITE_CALC_OPERATIONAL_API_KEY")


@pytest.fixture
def live() -> OperationalClient:
    if not URL or not KEY:
        pytest.skip("SITE_CALC_OPERATIONAL_URL / SITE_CALC_OPERATIONAL_API_KEY not set")
    return OperationalClient(URL, KEY)


def test_full_day(live: OperationalClient, site, day, forecast) -> None:  # type: ignore[no-untyped-def]
    with live as client:
        health = client.health()
        assert health.db_ok
        print(f"server {health.service_version}, engine {health.site_calc_version} @ {health.site_calc_commit_sha}")

        req = PlanReservationRequest(
            site=site,
            day=day,
            services=["afrr_plus", "afrr_minus"],
            ans_forecast=forecast,
            params=ReservationParams(planner="baseline"),
        )
        key = f"client-prod-{uuid.uuid4()}"
        plan = client.plan_reservation(req, idempotency_key=key)
        assert 1 <= len(plan.bids) <= 12
        assert plan.run.planner == "baseline" and plan.run.horizon_qh == 192
        print(f"{len(plan.bids)} bids, expected {plan.expected_revenue.total:.2f} EUR in {plan.run.solve_seconds} s")

        again = client.plan_reservation(req, idempotency_key=key)
        assert client.last_call_was_replay and again == plan

        cleared = [
            ClearedReservation(service=c.service, block_index=c.block_index, volume_mw=c.volume_mw)
            for c in plan.most_probable_realization.contracts
        ]
        da = client.plan_day_ahead(PlanDayAheadRequest(site=site, day=day, cleared_reservations=cleared))
        assert len(da.bids) == 96 and da.schedule.committed_qh == 96
        assert da.soc_end_mwh is not None and 0.0 <= da.soc_end_mwh <= site.battery.capacity_mwh
        print(f"day-ahead objective {da.objective_eur:.2f} EUR, soc_end {da.soc_end_mwh:.3f} MWh")

        with pytest.raises(DayNotPlannableError):
            client.plan_day_ahead(
                PlanDayAheadRequest(site=site, day=day.model_copy(update={"da_price_eur_per_mwh_d1": None}))
            )

        page = client.list_runs(limit=5)
        assert {r.endpoint for r in page.runs} >= {"plan-reservation", "plan-day-ahead"}
        assert client.cancel_active() is False
