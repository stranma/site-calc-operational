"""Plan one day for a 1 MW / 2 MWh battery: reservation bids, then day-ahead bids.

    SITE_CALC_OPERATIONAL_URL=https://... SITE_CALC_OPERATIONAL_API_KEY=op_... python examples/plan_bess_day.py

The prices and the acceptance forecast below are made up; replace them with
your own forecasts. The script prints the bids and the end-of-day state of
charge you would carry into tomorrow's ``initial_soc_mwh``.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, timedelta

from site_calc_operational import (
    ANSAbility,
    AnsForecastEntry,
    Battery,
    ClearedReservation,
    Day,
    ElectricityExport,
    ElectricityImport,
    LogNormal,
    OperationalClient,
    OperationalError,
    PlanDayAheadRequest,
    PlanReservationRequest,
    ReservationParams,
    Site,
)


def main() -> int:
    url = os.environ.get("SITE_CALC_OPERATIONAL_URL")
    key = os.environ.get("SITE_CALC_OPERATIONAL_API_KEY")
    if not url or not key:
        print("set SITE_CALC_OPERATIONAL_URL and SITE_CALC_OPERATIONAL_API_KEY", file=sys.stderr)
        return 2

    site = Site(
        site_id="demo-bess",
        devices=[
            Battery(
                name="BESS",
                capacity_mwh=2.0,
                max_power_mw=1.0,
                efficiency=0.9,
                initial_soc_mwh=1.0,  # tomorrow: yesterday's plan.soc_end_mwh
                ans_abilities=[
                    ANSAbility(service="afrr_plus", min_device_power_rate=0.0, max_device_power_rate=1.0),
                    ANSAbility(service="afrr_minus", min_device_power_rate=0.0, max_device_power_rate=1.0),
                ],
            ),
            ElectricityImport(name="GridBuy", max_import_mw=1.0, buy_fee_eur_per_mwh=1.5),
            ElectricityExport(name="GridSell", max_export_mw=1.0, sell_fee_eur_per_mwh=1.0),
        ],
    )

    delivery_day = date.today() + timedelta(days=1)
    # 96 quarter-hour prices per day; here a crude shape: cheap at night, dear at noon and evening.
    shape = [40.0 + 60.0 * (7 <= h <= 9 or 17 <= h <= 20) for h in range(24)]
    prices = [p for p in shape for _ in range(4)]
    day = Day(date=delivery_day, tz="Europe/Prague", da_price_eur_per_mwh_d=prices, da_price_eur_per_mwh_d1=prices)

    forecast = [
        AnsForecastEntry(
            service=service,
            block_index=block,
            distribution=LogNormal(mu=2.0, sigma=0.5),  # median clearing price e^2 = 7.4 EUR/MW/h
            expected_activation_eur_per_mw_h=1.0,
        )
        for service in ("afrr_plus", "afrr_minus")
        for block in range(6)
    ]

    with OperationalClient(url, key) as client:
        health = client.health()
        print(f"server {health.service_version} ({health.status})")

        try:
            plan = client.plan_reservation(
                PlanReservationRequest(
                    site=site,
                    day=day,
                    services=["afrr_plus", "afrr_minus"],
                    ans_forecast=forecast,
                    params=ReservationParams(planner="baseline"),  # "sitecalc" is better and slower
                ),
                idempotency_key=f"demo-reservation-{delivery_day}-{uuid.uuid4()}",
            )
        except OperationalError as exc:
            print(f"reservation step failed: {exc}", file=sys.stderr)
            return 1

        print(f"\nreservation bids for {delivery_day} ({plan.run.solve_seconds} s):")
        for bid in plan.bids:
            print(
                f"  {bid.service:<11} block {bid.block_index}  {bid.volume_mw:5.2f} MW"
                f" @ {bid.capacity_price_eur_per_mw_h:6.2f} EUR/MW/h"
                f"  p(clear)={bid.forecast_clear_probability:.2f}"
            )
        rev = plan.expected_revenue
        print(
            f"expected: reservation {rev.reservation:.1f} + activation {rev.activation:.1f} "
            f"+ day-ahead {rev.da_arbitrage:.1f} = {rev.total:.1f} EUR"
        )

        # After the reservation auction clears, feed the actual contracts in. Here we
        # pretend the most probable outcome is what cleared.
        cleared = [
            ClearedReservation(service=c.service, block_index=c.block_index, volume_mw=c.volume_mw)
            for c in plan.most_probable_realization.contracts
        ]
        da = client.plan_day_ahead(PlanDayAheadRequest(site=site, day=day, cleared_reservations=cleared))

        print(f"\nday-ahead bids (first 8 of {len(da.bids)}):")
        for bid in da.bids[:8]:
            side = "sell" if bid.volume_mw > 0 else "buy " if bid.volume_mw < 0 else "----"
            print(f"  {bid.interval_start:%H:%M}  {side} {abs(bid.volume_mw):5.2f} MW")
        print(
            f"planned value {da.objective_eur:.1f} EUR (fees {da.market_fees_eur:.1f}); "
            f"state of charge at midnight {da.soc_end_mwh:.3f} MWh"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
