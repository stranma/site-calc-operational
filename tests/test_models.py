"""The request models catch what the server would reject, before any HTTP happens."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from site_calc_operational import (
    CHP,
    ANSAbility,
    AnsForecastEntry,
    Battery,
    ClearedReservation,
    Day,
    DayAheadPlan,
    ElectricityExport,
    EmpiricalPercentiles,
    GasImport,
    LogNormal,
    LogNormalFromQuantiles,
    PlanDayAheadRequest,
    PlanReservationRequest,
    ReservationPlan,
    Site,
)


def test_site_requires_export_and_unique_types() -> None:
    """Failure mode: a site without a grid sell leg would be a server-side 422 instead of an instant error."""
    battery = Battery(name="b", capacity_mwh=1, max_power_mw=1, efficiency=0.9, initial_soc_mwh=0)
    with pytest.raises(PydanticValidationError, match="electricity_export"):
        Site(site_id="s", devices=[battery])
    with pytest.raises(PydanticValidationError, match="one device per type"):
        Site(
            site_id="s",
            devices=[ElectricityExport(name="a"), ElectricityExport(name="b")],
        )


def test_site_chp_needs_gas_and_single_ans_device() -> None:
    chp = CHP(name="chp", gas_input_mw=2.5, el_output_mw=1.0, heat_output_mw=1.3)
    with pytest.raises(PydanticValidationError, match="gas_import"):
        Site(site_id="s", devices=[chp, ElectricityExport(name="el")])
    ability = ANSAbility(service="afrr_plus", min_device_power_rate=0.0, max_device_power_rate=1.0)
    chp_ans = CHP(name="chp", gas_input_mw=2.5, el_output_mw=1.0, heat_output_mw=1.3, ans_abilities=[ability])
    bess_ans = Battery(
        name="b", capacity_mwh=1, max_power_mw=1, efficiency=0.9, initial_soc_mwh=0, ans_abilities=[ability]
    )
    with pytest.raises(PydanticValidationError, match="only one device may declare ans_abilities"):
        Site(
            site_id="s",
            devices=[chp_ans, GasImport(name="gas", price_eur_per_mwh=30.0), bess_ans, ElectricityExport(name="el")],
        )


def test_battery_soc_and_ability_window() -> None:
    with pytest.raises(PydanticValidationError, match="initial_soc_mwh must not exceed"):
        Battery(name="b", capacity_mwh=1.0, max_power_mw=1.0, efficiency=0.9, initial_soc_mwh=1.5)
    with pytest.raises(PydanticValidationError, match="below max_device_power_rate"):
        ANSAbility(service="afrr_plus", min_device_power_rate=0.5, max_device_power_rate=0.5)


def test_site_battery_property(site: Site) -> None:
    assert site.battery is not None and site.battery.name == "BESS"
    assert Site(site_id="x", devices=[ElectricityExport(name="el")]).battery is None


def test_unknown_field_is_rejected() -> None:
    """Failure mode: a typo like ``capacity_mw`` silently dropped would plan the wrong battery."""
    with pytest.raises(PydanticValidationError, match="extra"):
        Battery(name="b", capacity_mw=1.0, max_power_mw=1.0, efficiency=0.9, initial_soc_mwh=0)  # type: ignore[call-arg]


def test_day_price_lengths() -> None:
    with pytest.raises(PydanticValidationError):
        Day(date="2026-04-15", tz="Europe/Prague", da_price_eur_per_mwh_d=[1.0] * 24)
    d = Day(date="2026-04-15", tz="Europe/Prague", da_price_eur_per_mwh_d=[1.0] * 96)
    assert d.da_price_eur_per_mwh_d1 is None


def test_distributions_discriminate_on_type() -> None:
    e = AnsForecastEntry(
        service="afrr_plus", block_index=0, distribution={"type": "lognormal", "mu": 1.0, "sigma": 0.3}
    )
    assert isinstance(e.distribution, LogNormal)
    e = AnsForecastEntry(
        service="afrr_plus",
        block_index=0,
        distribution={"type": "lognormal_from_quantiles", "quantiles": [[0.1, 3.0], [0.9, 12.0]]},
    )
    assert isinstance(e.distribution, LogNormalFromQuantiles)
    e = AnsForecastEntry(
        service="afrr_plus",
        block_index=0,
        distribution={"type": "empirical_percentiles", "breakpoints": [[0.0, 1.0], [20.0, 0.0]]},
    )
    assert isinstance(e.distribution, EmpiricalPercentiles)
    with pytest.raises(PydanticValidationError):
        AnsForecastEntry(service="afrr_plus", block_index=6, distribution=LogNormal(mu=1.0, sigma=0.3))


def test_reservation_request_forecast_coverage(site: Site, day: Day, forecast: list[AnsForecastEntry]) -> None:
    """Failure mode: a missing block would be a VALIDATION_ERROR from the server after a round trip."""
    with pytest.raises(PydanticValidationError, match="missing entries"):
        PlanReservationRequest(site=site, day=day, services=["afrr_plus", "mfrr_plus"], ans_forecast=forecast)
    with pytest.raises(PydanticValidationError, match="duplicate"):
        PlanReservationRequest(site=site, day=day, services=["afrr_plus"], ans_forecast=forecast + forecast[:1])
    req = PlanReservationRequest(site=site, day=day, services=["afrr_plus"], ans_forecast=forecast)
    assert req.params.planner == "sitecalc"


def test_reservation_request_wire_shape(reservation_request: PlanReservationRequest) -> None:
    """The dict form is exactly what the server accepts: no None-valued optionals, dates as strings."""
    wire = reservation_request.model_dump(mode="json", exclude_none=True)
    assert wire["day"]["date"] == "2026-04-15"
    assert wire["site"]["devices"][0]["type"] == "battery"
    assert "metadata" not in wire
    assert wire["params"] == {"planner": "sitecalc", "assume_maximal": False, "px_percentile": 0.5}


def test_day_ahead_request(site: Site, day: Day) -> None:
    req = PlanDayAheadRequest(
        site=site,
        day=day,
        cleared_reservations=[ClearedReservation(service="afrr_plus", block_index=1, volume_mw=0.6)],
        chp_pins={2: 0.75},
    )
    wire = req.model_dump(mode="json", exclude_none=True)
    assert wire["cleared_reservations"] == [{"service": "afrr_plus", "block_index": 1, "volume_mw": 0.6}]
    assert wire["chp_pins"] == {"2": 0.75}
    with pytest.raises(PydanticValidationError):
        ClearedReservation(service="afrr_plus", block_index=1, volume_mw=0.0)


def test_response_models_tolerate_new_fields(reservation_plan_body: dict, day_ahead_plan_body: dict) -> None:
    """Failure mode: a server that adds a field must not break older clients."""
    body = {**reservation_plan_body, "new_field": 1}
    plan = ReservationPlan.model_validate(body)
    assert plan.bids[0].forecast_clear_probability == 0.62
    assert plan.most_probable_realization.contracts[0].forecast_clear_probability is None
    assert plan.bids[0].interval_start.isoformat() == "2026-04-15T08:00:00+02:00"
    da = DayAheadPlan.model_validate({**day_ahead_plan_body, "schedule": {**day_ahead_plan_body["schedule"], "x": 1}})
    assert len(da.bids) == 96 and da.soc_end_mwh == 0.75 and da.schedule.committed_qh == 96
