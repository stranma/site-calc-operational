"""Tests for the typed device-properties + typed-device models.

Coverage:

* Construction-time validation per device type (positive scalars, [0,1]
  fractions, etc.).
* ``model_dump`` produces a JSON shape the on-prem server's ``translate_device``
  reads (full ``DeviceRequest(name, type, properties=...)`` round-trip).
* The ``TypedDevice`` discriminated union dispatches by ``type``.
* Field-name parity against what the on-prem server's translate_device reads
  from ``properties``.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from site_calc_operational.models import (
    ANSAbility,
    BatteryDevice,
    BatteryProperties,
    CHPDevice,
    CHPProperties,
    DeviceRequest,
    ElectricityExportDevice,
    ElectricityExportProperties,
    ElectricityImportDevice,
    ElectricityImportProperties,
    GasImportDevice,
    GasImportProperties,
    HeatAccumulatorDevice,
    HeatAccumulatorProperties,
    HeatDemandDevice,
    HeatDemandProperties,
    HeatExportDevice,
    HeatExportProperties,
    TypedDevice,
)

# ---------------------------------------------------------------------------
# ANSAbility validation (mirrors site_calc.domain.ans.ANSAbility)
# ---------------------------------------------------------------------------


def test_ansability_min_must_be_geq_neg_one() -> None:
    with pytest.raises(ValidationError):
        ANSAbility(service="afrr_plus", min_device_power_rate=-1.5, max_device_power_rate=1.0)


def test_ansability_max_must_be_leq_one() -> None:
    with pytest.raises(ValidationError):
        ANSAbility(service="afrr_plus", min_device_power_rate=0.0, max_device_power_rate=1.5)


def test_ansability_max_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        ANSAbility(service="afrr_plus", min_device_power_rate=0.0, max_device_power_rate=0.0)


def test_ansability_battery_swing_allowed() -> None:
    """A battery can swing from full charge (-1) to full discharge (+1) while
    serving a single direction-symmetric service. The constraint should permit
    that."""
    ANSAbility(service="afrr_plus", min_device_power_rate=-1.0, max_device_power_rate=1.0)


# ---------------------------------------------------------------------------
# Per-type properties validation
# ---------------------------------------------------------------------------


def test_battery_requires_positive_capacity_and_power() -> None:
    with pytest.raises(ValidationError):
        BatteryProperties(capacity=0.0, max_power=5.0, efficiency=0.9)
    with pytest.raises(ValidationError):
        BatteryProperties(capacity=10.0, max_power=0.0, efficiency=0.9)


def test_battery_efficiency_must_be_in_zero_one() -> None:
    with pytest.raises(ValidationError):
        BatteryProperties(capacity=10.0, max_power=5.0, efficiency=1.5)
    with pytest.raises(ValidationError):
        BatteryProperties(capacity=10.0, max_power=5.0, efficiency=0.0)


def test_battery_initial_soc_bounded() -> None:
    with pytest.raises(ValidationError):
        BatteryProperties(capacity=10.0, max_power=5.0, efficiency=0.9, initial_soc=1.5)


def test_chp_requires_positive_outputs() -> None:
    """CHP gas_input / el_output / heat_output must all be positive."""
    with pytest.raises(ValidationError):
        CHPProperties(gas_input=0.0, el_output=1.0, heat_output=1.0)
    with pytest.raises(ValidationError):
        CHPProperties(gas_input=2.5, el_output=0.0, heat_output=1.0)


def test_chp_max_starts_must_be_non_negative() -> None:
    with pytest.raises(ValidationError):
        CHPProperties(gas_input=2.5, el_output=1.0, heat_output=1.0, max_starts_per_day=-1)


def test_chp_ans_abilities_default_empty() -> None:
    """No ans_abilities means the device is invisible to the reservation-bid
    planner (which raises 'no ANS-capable device'). Default must be empty,
    not None, so callers can append."""
    chp = CHPProperties(gas_input=2.5, el_output=1.0, heat_output=1.0)
    assert chp.ans_abilities == []


def test_gas_import_max_import_total_optional_and_non_negative() -> None:
    GasImportProperties(price=[45.0] * 96, max_import=2.5)  # ok without budget
    GasImportProperties(price=[45.0] * 96, max_import=2.5, max_import_total=15.0)  # ok with
    with pytest.raises(ValidationError):
        GasImportProperties(price=[45.0] * 96, max_import=2.5, max_import_total=-1.0)


def test_unknown_field_rejected_under_extra_forbid() -> None:
    """Each properties model is ``extra='forbid'`` so a misspelled field is
    caught at construction time rather than silently dropped on the wire."""
    with pytest.raises(ValidationError):
        CHPProperties(gas_input=2.5, el_output=1.0, heat_output=1.0, max_starts_per_dy=3)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# TypedDevice discriminated union
# ---------------------------------------------------------------------------


def test_typed_device_dispatches_by_type() -> None:
    """A dict with ``type="chp"`` parses as CHPDevice (not BatteryDevice),
    with the right properties subclass."""
    adapter = TypeAdapter(TypedDevice)
    raw = {
        "name": "CHP-bin",
        "type": "chp",
        "properties": {
            "gas_input": 2.5,
            "el_output": 1.0,
            "heat_output": 1.0,
            "is_binary": True,
            "ans_abilities": [
                {"service": "afrr_plus", "min_device_power_rate": 0.0, "max_device_power_rate": 1.0},
            ],
        },
    }
    parsed = adapter.validate_python(raw)
    assert isinstance(parsed, CHPDevice)
    assert isinstance(parsed.properties, CHPProperties)
    assert parsed.properties.gas_input == 2.5
    assert parsed.properties.ans_abilities[0].service == "afrr_plus"


def test_typed_device_battery_parses() -> None:
    adapter = TypeAdapter(TypedDevice)
    parsed = adapter.validate_python(
        {"name": "Bat", "type": "battery", "properties": {"capacity": 6.0, "max_power": 3.0, "efficiency": 0.9}}
    )
    assert isinstance(parsed, BatteryDevice)
    assert isinstance(parsed.properties, BatteryProperties)


def test_typed_device_unknown_type_rejected() -> None:
    """A type the union doesn't list (e.g. ``photovoltaic`` which the on-prem
    server explicitly rejects) must fail at parse time, not at the server."""
    adapter = TypeAdapter(TypedDevice)
    with pytest.raises(ValidationError):
        adapter.validate_python({"name": "PV", "type": "photovoltaic", "properties": {"peak_power_mw": 1.0}})


# ---------------------------------------------------------------------------
# Wire-shape round trip: typed -> dict -> DeviceRequest -> JSON
# ---------------------------------------------------------------------------


def test_chp_properties_dump_matches_device_request_shape() -> None:
    """The dict produced by CHPProperties.model_dump must be exactly the
    ``properties`` value the on-prem server's translate_device('chp') reads."""
    chp = CHPProperties(
        gas_input=2.5,
        el_output=1.0,
        heat_output=1.0,
        is_binary=True,
        max_starts_per_day=3,
        ans_abilities=[
            ANSAbility(service="afrr_plus", min_device_power_rate=0.0, max_device_power_rate=1.0),
            ANSAbility(service="afrr_minus", min_device_power_rate=0.0, max_device_power_rate=1.0),
        ],
    )
    device = DeviceRequest(name="CHP-bin", type="chp", properties=chp.model_dump(mode="json"))
    wire = device.model_dump(mode="json")
    assert wire["type"] == "chp"
    assert wire["properties"]["gas_input"] == 2.5
    assert wire["properties"]["ans_abilities"][0]["service"] == "afrr_plus"
    # Field names match what server's translate_device('chp') consumes.
    expected_props_keys = {"gas_input", "el_output", "heat_output", "is_binary", "max_starts_per_day", "ans_abilities"}
    assert set(wire["properties"].keys()) == expected_props_keys


def test_typed_device_dumps_to_same_shape_as_device_request() -> None:
    """A CHPDevice and an equivalent DeviceRequest(type='chp', properties=...)
    must serialize to the same wire JSON. This is the back-compat guarantee:
    a site can mix typed devices and DeviceRequest dicts without the server
    seeing a difference."""
    chp_props = CHPProperties(gas_input=2.5, el_output=1.0, heat_output=1.0)
    typed = CHPDevice(name="CHP-bin", properties=chp_props)
    loose = DeviceRequest(name="CHP-bin", type="chp", properties=chp_props.model_dump(mode="json"))

    typed_dump = typed.model_dump(mode="json")
    loose_dump = loose.model_dump(mode="json")
    # DeviceRequest has extra optional fields (schedule, ancillary_services); compare what's present.
    assert typed_dump["name"] == loose_dump["name"]
    assert typed_dump["type"] == loose_dump["type"]
    assert typed_dump["properties"] == loose_dump["properties"]


# ---------------------------------------------------------------------------
# Field-name parity with the on-prem server's translate_device
# ---------------------------------------------------------------------------


# Field names the on-prem server's translate_device reads from properties[type].
# When the server's translation logic changes, this constant lags and the
# parity test below fails -- update both sides in lockstep.
_SERVER_TRANSLATE_DEVICE_PROPS: dict[str, set[str]] = {
    "battery": {"capacity", "max_power", "efficiency", "initial_soc", "soc_anchor_interval_hours", "soc_anchor_target"},
    "heat_accumulator": {"capacity", "max_power", "efficiency", "initial_soc", "loss_rate"},
    "chp": {"gas_input", "el_output", "heat_output", "is_binary", "max_starts_per_day", "ans_abilities"},
    "heat_demand": {"max_demand_profile", "min_demand_profile", "demand_profile"},
    "electricity_import": {"price", "max_import"},
    "electricity_export": {"price", "max_export"},
    "gas_import": {"price", "max_import", "max_import_total"},
    "heat_export": {"price", "max_export"},
}

_PROPS_CLASS_BY_TYPE: dict[str, type] = {
    "battery": BatteryProperties,
    "heat_accumulator": HeatAccumulatorProperties,
    "chp": CHPProperties,
    "heat_demand": HeatDemandProperties,
    "electricity_import": ElectricityImportProperties,
    "electricity_export": ElectricityExportProperties,
    "gas_import": GasImportProperties,
    "heat_export": HeatExportProperties,
}


def test_property_field_parity_with_server_translator() -> None:
    """Failure mode: client adds a property field the server's translate_device
    doesn't read (silently dropped on the wire), or the server adds one the
    client doesn't expose (callers can't reach the new behaviour)."""
    for device_type, expected in _SERVER_TRANSLATE_DEVICE_PROPS.items():
        cls = _PROPS_CLASS_BY_TYPE[device_type]
        actual = set(cls.model_fields.keys())
        assert actual == expected, (
            f"{cls.__name__} field set differs from server-onprem translate_device({device_type!r}):\n"
            f"  expected: {sorted(expected)}\n"
            f"  actual:   {sorted(actual)}\n"
            f"  missing:  {sorted(expected - actual)}\n"
            f"  extra:    {sorted(actual - expected)}"
        )


def test_site_request_accepts_typed_devices() -> None:
    """SiteRequest.devices must accept the typed device wrappers directly,
    not just the loose DeviceRequest. Otherwise the typed-device classes
    shipped in v0.2.1 are dead weight."""
    from site_calc_operational.models import SiteRequest

    site = SiteRequest(
        site_id="s1",
        devices=[
            CHPDevice(
                name="CHP",
                properties=CHPProperties(
                    gas_input=2.5,
                    el_output=1.0,
                    heat_output=1.0,
                    ans_abilities=[
                        ANSAbility(service="afrr_plus", min_device_power_rate=0.0, max_device_power_rate=1.0),
                    ],
                ),
            ),
        ],
    )
    assert isinstance(site.devices[0], CHPDevice)
    assert isinstance(site.devices[0].properties, CHPProperties)


def test_site_request_accepts_loose_device_requests() -> None:
    """Back-compat: SiteRequest.devices still accepts the generic DeviceRequest
    so unknown device types (or types not yet modelled) flow through."""
    from site_calc_operational.models import DeviceRequest, SiteRequest

    site = SiteRequest(
        site_id="s1",
        devices=[DeviceRequest(name="Bar", type="something_unknown", properties={"k": 1})],
    )
    assert isinstance(site.devices[0], DeviceRequest)
    assert site.devices[0].type == "something_unknown"


def test_site_request_mixes_typed_and_loose() -> None:
    """A single SiteRequest can carry a mix of typed and dict-based devices.
    Each device is parsed to its appropriate class."""
    from site_calc_operational.models import DeviceRequest, SiteRequest

    site = SiteRequest(
        site_id="s1",
        devices=[
            CHPDevice(
                name="CHP",
                properties=CHPProperties(gas_input=2.5, el_output=1.0, heat_output=1.0),
            ),
            DeviceRequest(name="Bar", type="unknown_kind", properties={}),
        ],
    )
    assert isinstance(site.devices[0], CHPDevice)
    assert isinstance(site.devices[1], DeviceRequest)


def test_site_request_parse_dispatches_known_types_to_typed_devices() -> None:
    """When parsing from JSON, known type literals dispatch to the typed
    subclass; unknown types fall through to DeviceRequest."""
    from site_calc_operational.models import DeviceRequest, SiteRequest

    site = SiteRequest.model_validate(
        {
            "site_id": "s1",
            "devices": [
                {
                    "name": "CHP",
                    "type": "chp",
                    "properties": {"gas_input": 2.5, "el_output": 1.0, "heat_output": 1.0},
                },
                {"name": "Bar", "type": "future_type", "properties": {}},
            ],
        }
    )
    assert isinstance(site.devices[0], CHPDevice)
    assert isinstance(site.devices[1], DeviceRequest)


def test_typed_device_type_literal_matches_class() -> None:
    """The ``type`` literal on each typed device must be the string the server
    expects -- e.g. CHPDevice.type == 'chp', not 'CHP'."""
    cases = [
        (BatteryDevice, "battery"),
        (HeatAccumulatorDevice, "heat_accumulator"),
        (CHPDevice, "chp"),
        (HeatDemandDevice, "heat_demand"),
        (ElectricityImportDevice, "electricity_import"),
        (ElectricityExportDevice, "electricity_export"),
        (GasImportDevice, "gas_import"),
        (HeatExportDevice, "heat_export"),
    ]
    for cls, expected_type in cases:
        # Pydantic stores the literal as the default for the ``type`` field.
        default = cls.model_fields["type"].default
        assert default == expected_type, f"{cls.__name__}.type default is {default!r}, expected {expected_type!r}"
