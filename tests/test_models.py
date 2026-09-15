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
    Composite,
    Day,
    DayAheadPlan,
    ElectricityExport,
    EmpiricalPercentiles,
    GasImport,
    LogNormal,
    LogNormalFromQuantiles,
    PlanDayAheadRequest,
    PlanReservationRequest,
    Profile,
    ReservationPlan,
    Site,
    Storage,
)


def test_site_accepts_repeated_types_and_import_only_sites() -> None:
    battery = Battery(name="b", capacity_mwh=1, max_power_mw=1, efficiency=0.9, initial_soc_mwh=0)
    assert Site(site_id="s", devices=[battery]).has_ler
    assert len(Site(site_id="s", devices=[ElectricityExport(name="a"), ElectricityExport(name="b")]).devices) == 2
    with pytest.raises(PydanticValidationError, match="names must be unique"):
        Site(site_id="s", devices=[battery, battery])


def test_site_chp_needs_gas_and_single_ans_device() -> None:
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
        Day(date="2026-04-15", tz="Europe/Prague", da_price_eur_per_mwh_d=[1.0] * 95)
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
        PlanReservationRequest(site=site, day=day, services=["afrr_plus", "afrr_minus"], ans_forecast=forecast[:6])
    with pytest.raises(PydanticValidationError, match="not in any device's ans_abilities"):
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


def test_battery_site_needs_next_day_prices(site: Site, day: Day, forecast: list[AnsForecastEntry]) -> None:
    """Failure mode: forgetting D+1 prices for a battery would only surface after a server round trip."""
    one_day = day.model_copy(update={"da_price_eur_per_mwh_d1": None})
    with pytest.raises(PydanticValidationError, match="da_price_eur_per_mwh_d1"):
        PlanReservationRequest(site=site, day=one_day, services=["afrr_plus"], ans_forecast=forecast)
    with pytest.raises(PydanticValidationError, match="da_price_eur_per_mwh_d1"):
        PlanDayAheadRequest(site=site, day=one_day)
    chp_site = Site(
        site_id="chp",
        devices=[
            CHP(name="chp", gas_input_mw=2.5, el_output_mw=1.0, heat_output_mw=1.3),
            GasImport(name="gas", price_eur_per_mwh=30.0),
            ElectricityExport(name="el"),
        ],
    )
    assert PlanDayAheadRequest(site=chp_site, day=one_day).day.da_price_eur_per_mwh_d1 is None


def test_day_ahead_request(site: Site, day: Day) -> None:
    req = PlanDayAheadRequest(
        site=site,
        day=day,
        cleared_reservations=[ClearedReservation(service="afrr_plus", block_index=1, volume_mw=0.6)],
    )
    wire = req.model_dump(mode="json", exclude_none=True)
    assert wire["cleared_reservations"] == [{"service": "afrr_plus", "block_index": 1, "volume_mw": 0.6}]
    assert wire["chp_pins"] == {}
    with pytest.raises(PydanticValidationError):
        ClearedReservation(service="afrr_plus", block_index=1, volume_mw=0.0)
    # rules the server would otherwise reject after a round trip
    mfrr = ClearedReservation(service="mfrr_plus", block_index=0, volume_mw=1)
    with pytest.raises(PydanticValidationError, match="no device declares"):
        PlanDayAheadRequest(site=site, day=day, cleared_reservations=[mfrr])
    with pytest.raises(PydanticValidationError, match="no chp device"):
        PlanDayAheadRequest(site=site, day=day, chp_pins={2: 0.75})
    chp_site = Site(
        site_id="chp",
        devices=[
            CHP(name="chp", gas_input_mw=2.5, el_output_mw=1.0, heat_output_mw=1.3),
            GasImport(name="gas", price_eur_per_mwh=30.0),
            ElectricityExport(name="el"),
        ],
    )
    wire = PlanDayAheadRequest(site=chp_site, day=day, chp_pins={2: 0.75}).model_dump(mode="json")
    assert wire["chp_pins"] == {"2": 0.75}


def test_distribution_shape_rules() -> None:
    """Failure mode: the two pair orders are opposite; a swapped pair must not reach the server."""
    with pytest.raises(PydanticValidationError, match="strictly between 0 and 1"):
        LogNormalFromQuantiles(quantiles=[(3.0, 0.1), (12.0, 0.9)])  # price, probability: swapped
    with pytest.raises(PydanticValidationError, match="distinct"):
        LogNormalFromQuantiles(quantiles=[(0.5, 3.0), (0.5, 12.0)])
    with pytest.raises(PydanticValidationError, match="strictly ascending"):
        EmpiricalPercentiles(breakpoints=[(20.0, 0.0), (0.0, 1.0)])
    with pytest.raises(PydanticValidationError, match="between 0 and 1"):
        EmpiricalPercentiles(breakpoints=[(0.0, 1.0), (20.0, 1.5)])


def test_response_models_tolerate_new_fields(reservation_plan_body: dict, day_ahead_plan_body: dict) -> None:
    """Failure mode: a server that adds a field must not break older clients."""
    body = {**reservation_plan_body, "new_field": 1}
    plan = ReservationPlan.model_validate(body)
    assert plan.bids[0].forecast_clear_probability == 0.62
    assert plan.most_probable_realization.contracts[0].forecast_clear_probability is None
    assert plan.bids[0].interval_start.isoformat() == "2026-04-15T08:00:00+02:00"
    da = DayAheadPlan.model_validate({**day_ahead_plan_body, "schedule": {**day_ahead_plan_body["schedule"], "x": 1}})
    assert len(da.bids) == 96 and da.soc_end_mwh == 0.75 and da.schedule.committed_qh == 96


def test_general_profiles_and_thermal_horizon(day: Day) -> None:
    heat = Profile(type="heat_demand", name="load", maximum_mw=[1.0] * 192)
    assert heat.material == "heat"
    with pytest.raises(PydanticValidationError, match="requires material"):
        Profile(type="electricity_demand", material="heat", name="wrong", maximum_mw=[1.0] * 96)
    with pytest.raises(PydanticValidationError, match="min_total_mwh"):
        Profile(type="generic_supply", name="wrong", maximum_mw=[1.0], min_total_mwh=2, max_total_mwh=1)
    store = Storage(name="tank", capacity_mwh=5, max_power_mw=2, efficiency=1, initial_soc_mwh=3)
    site = Site(site_id="heat", devices=[heat, store])
    assert site.has_ler and site.battery is None
    with pytest.raises(PydanticValidationError, match="da_price_eur_per_mwh_d1"):
        PlanDayAheadRequest(site=site, day=day.model_copy(update={"da_price_eur_per_mwh_d1": None}))
    assert PlanDayAheadRequest(site=site, day=day).site.devices[0].name == "load"


def test_composite_rejects_unsupported_piece_constraints() -> None:
    idle = Profile(type="electricity_supply", name="idle", maximum_mw=[0.0] * 96)
    active = CHP(name="active", gas_input_mw=2, el_output_mw=1, heat_output_mw=0, max_starts_per_day=1)
    with pytest.raises(PydanticValidationError, match="cumulative or temporal"):
        Composite(name="unit", sub_devices=[idle, active])
