"""Typed device-properties models, mirroring the site-calc domain devices.

Construct typed properties for each device type and dump them into a
:class:`DeviceRequest`'s ``properties`` field. Gives IDE autocomplete and
Pydantic validation for the per-type field set without breaking the existing
``DeviceRequest`` shape (which still accepts ``dict[str, Any]``).

Usage:

    chp = CHPProperties(
        gas_input=2.5,
        el_output=1.0,
        heat_output=1.0,
        is_binary=True,
        ans_abilities=[
            ANSAbility(service="afrr_plus", min_device_power_rate=0.0, max_device_power_rate=1.0),
        ],
    )
    device = DeviceRequest(
        name="CHP-bin",
        type="chp",
        properties=chp.model_dump(mode="json"),
    )

Field names and constraints mirror what the on-prem server's
``translate_device`` consumes (see
``server-onprem/src/site_calc_onprem/translation.py``) and what the
underlying domain dataclasses in ``site_calc.domain.devices.*`` accept.
Drift is pinned by ``test_device_property_models.py``.

Device types not modelled here:

* ``photovoltaic`` and ``electricity_demand`` -- the on-prem server explicitly
  rejects these (see ``translate_device``).
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from site_calc_operational.models.reservation_bids import ServiceCode

# ---------------------------------------------------------------------------
# ANS ability -- shared building block for CHP and (future) Battery ans services
# ---------------------------------------------------------------------------


class ANSAbility(BaseModel):
    """Device's prequalified ability to provide a specific ancillary service.

    Mirrors ``site_calc.domain.ans.ANSAbility``. Rates are fractions of
    device nominal capacity:

    * ``max_device_power_rate`` in (0, 1] -- upper edge of the ability's
      power window.
    * ``min_device_power_rate`` in [-1, 1) -- lower edge. May be negative
      for direction-symmetric devices (battery) to express a -P_nom..+P_nom
      swing (effective service capacity up to 2 * P_nom).

    Construction-time validation matches the domain class; the on-prem
    server re-validates server-side.
    """

    service: ServiceCode
    min_device_power_rate: float = Field(..., ge=-1.0, lt=1.0)
    max_device_power_rate: float = Field(..., gt=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Profile shape -- per-interval values on the wire
# ---------------------------------------------------------------------------

# A profile is a flat list of per-interval values (length must match the
# timespan's interval count, enforced server-side). The MCP layer also accepts
# {"file": "...", "column": "..."} or a scalar broadcast; that's a scenario-
# store convenience that resolves before the wire request. SDK callers send
# the resolved list directly.
Profile = list[float]


# ---------------------------------------------------------------------------
# Per-device-type properties
# ---------------------------------------------------------------------------


class BatteryProperties(BaseModel):
    """Properties for ``DeviceRequest(type="battery")``. Mirrors
    ``site_calc.domain.devices.storage.Battery``.

    The battery is a bidirectional electricity storage. ``efficiency`` is the
    round-trip efficiency applied symmetrically to charge and discharge by
    the on-prem translator (``charge_efficiency = discharge_efficiency = sqrt(efficiency)``
    on the domain side).
    """

    model_config = ConfigDict(extra="forbid")

    capacity: float = Field(..., gt=0, description="Usable capacity (MWh).")
    max_power: float = Field(..., gt=0, description="Max charge/discharge power (MW).")
    efficiency: float = Field(..., gt=0, le=1.0, description="Round-trip efficiency (0..1].")
    initial_soc: float | None = Field(default=None, ge=0.0, le=1.0, description="Initial state-of-charge fraction.")
    soc_anchor_interval_hours: float | None = Field(
        default=None, gt=0, description="Hours between SOC return-to-anchor checkpoints."
    )
    soc_anchor_target: float | None = Field(
        default=None, ge=0.0, le=1.0, description="SOC the battery returns to at each anchor."
    )


class HeatAccumulatorProperties(BaseModel):
    """Properties for ``DeviceRequest(type="heat_accumulator")``. Mirrors
    ``site_calc.domain.devices.storage.HeatAccumulator``.
    """

    model_config = ConfigDict(extra="forbid")

    capacity: float = Field(..., gt=0, description="Usable thermal capacity (MWh).")
    max_power: float = Field(..., gt=0, description="Max charge/discharge thermal power (MW).")
    efficiency: float = Field(..., gt=0, le=1.0, description="Round-trip efficiency (0..1].")
    initial_soc: float | None = Field(default=None, ge=0.0, le=1.0)
    loss_rate: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Per-interval self-discharge fraction (0..1]."
    )


class CHPProperties(BaseModel):
    """Properties for ``DeviceRequest(type="chp")``. Mirrors
    ``site_calc.domain.devices.generator.CHP``.

    A CHP burns gas at ``gas_input`` MW and produces electricity at
    ``el_output`` MW + heat at ``heat_output`` MW simultaneously when running.
    Total efficiency ``(el_output + heat_output) / gas_input`` must be <= 1.0
    (server-side check). Set ``is_binary=True`` for an on/off unit;
    ``max_starts_per_day`` is only meaningful then.

    ``ans_abilities`` declares which ancillary services this unit can serve
    and over what fraction of its electrical output range. Required for the
    on-prem ``POST /v1/reservation-bids`` planner to pick this device.
    """

    model_config = ConfigDict(extra="forbid")

    gas_input: float = Field(..., gt=0, description="Gas consumption rate when on (MW).")
    el_output: float = Field(..., gt=0, description="Electricity output rate when on (MW).")
    heat_output: float = Field(..., gt=0, description="Heat output rate when on (MW).")
    is_binary: bool = Field(default=True, description="If True, unit is on/off; otherwise continuous in [0, 1].")
    max_starts_per_day: int | None = Field(
        default=None, ge=0, description="Max OFF->ON transitions over the horizon. Only meaningful when is_binary=True."
    )
    ans_abilities: list[ANSAbility] = Field(
        default_factory=list, description="Prequalified ANS abilities (used by the reservation-bid planner)."
    )


class HeatDemandProperties(BaseModel):
    """Properties for ``DeviceRequest(type="heat_demand")``. Mirrors
    ``site_calc.domain.devices.consumer.HeatDemand``.

    Two profile fields:

    * ``max_demand_profile`` -- upper bound on consumption per interval (MW).
    * ``min_demand_profile`` -- lower bound, defaults to 0.

    The on-prem server also accepts a single ``demand_profile`` alias that
    broadcasts to both bounds; prefer the two-field form when the demand has
    real slack.
    """

    model_config = ConfigDict(extra="forbid")

    max_demand_profile: Profile = Field(default_factory=list, description="Max consumption per interval (MW).")
    min_demand_profile: Profile | None = Field(default=None, description="Min consumption per interval (MW).")
    demand_profile: Profile | None = Field(
        default=None,
        description="Alias for ``max_demand_profile`` (server uses this when ``max_demand_profile`` is absent).",
    )


# ---------------------------------------------------------------------------
# Market interfaces
# ---------------------------------------------------------------------------


class ElectricityImportProperties(BaseModel):
    """Properties for ``DeviceRequest(type="electricity_import")``."""

    model_config = ConfigDict(extra="forbid")

    price: Profile = Field(..., description="EUR/MWh per interval.")
    max_import: float = Field(default=1000.0, gt=0, description="Max import power (MW).")


class ElectricityExportProperties(BaseModel):
    """Properties for ``DeviceRequest(type="electricity_export")``."""

    model_config = ConfigDict(extra="forbid")

    price: Profile = Field(..., description="EUR/MWh per interval received for export.")
    max_export: float = Field(default=1000.0, gt=0, description="Max export power (MW).")


class GasImportProperties(BaseModel):
    """Properties for ``DeviceRequest(type="gas_import")``.

    ``max_import_total`` is a cumulative MWh cap across the whole timespan --
    use it to express a daily fuel budget for a CHP. Omit (or set ``None``)
    for an unconstrained gas supply.
    """

    model_config = ConfigDict(extra="forbid")

    price: Profile = Field(..., description="EUR/MWh per interval.")
    max_import: float = Field(default=1000.0, gt=0, description="Max instantaneous gas flow (MW).")
    max_import_total: float | None = Field(
        default=None, ge=0, description="Cumulative gas budget across the timespan (MWh)."
    )


class HeatExportProperties(BaseModel):
    """Properties for ``DeviceRequest(type="heat_export")``."""

    model_config = ConfigDict(extra="forbid")

    price: Profile = Field(..., description="EUR/MWh per interval received for heat export.")
    max_export: float = Field(default=1000.0, gt=0, description="Max heat export power (MW).")


# ---------------------------------------------------------------------------
# Discriminated union over typed devices -- convenience for callers who want
# a single typed device value rather than constructing DeviceRequest by hand.
# ---------------------------------------------------------------------------


class BatteryDevice(BaseModel):
    """``DeviceRequest(type="battery")`` with typed properties.

    Convenience wrapper: ``BatteryDevice(name="Bat", properties=BatteryProperties(...))``
    serializes (via ``model_dump(mode="json")``) to the wire shape the
    on-prem server expects.
    """

    name: str = Field(..., min_length=1, max_length=100)
    type: Literal["battery"] = "battery"
    properties: BatteryProperties
    schedule: dict | None = None


class HeatAccumulatorDevice(BaseModel):
    """``DeviceRequest(type="heat_accumulator")`` with typed properties."""

    name: str = Field(..., min_length=1, max_length=100)
    type: Literal["heat_accumulator"] = "heat_accumulator"
    properties: HeatAccumulatorProperties
    schedule: dict | None = None


class CHPDevice(BaseModel):
    """``DeviceRequest(type="chp")`` with typed properties (incl. ans_abilities)."""

    name: str = Field(..., min_length=1, max_length=100)
    type: Literal["chp"] = "chp"
    properties: CHPProperties
    schedule: dict | None = None


class HeatDemandDevice(BaseModel):
    """``DeviceRequest(type="heat_demand")`` with typed properties."""

    name: str = Field(..., min_length=1, max_length=100)
    type: Literal["heat_demand"] = "heat_demand"
    properties: HeatDemandProperties
    schedule: dict | None = None


class ElectricityImportDevice(BaseModel):
    """``DeviceRequest(type="electricity_import")`` with typed properties."""

    name: str = Field(..., min_length=1, max_length=100)
    type: Literal["electricity_import"] = "electricity_import"
    properties: ElectricityImportProperties
    schedule: dict | None = None


class ElectricityExportDevice(BaseModel):
    """``DeviceRequest(type="electricity_export")`` with typed properties."""

    name: str = Field(..., min_length=1, max_length=100)
    type: Literal["electricity_export"] = "electricity_export"
    properties: ElectricityExportProperties
    schedule: dict | None = None


class GasImportDevice(BaseModel):
    """``DeviceRequest(type="gas_import")`` with typed properties."""

    name: str = Field(..., min_length=1, max_length=100)
    type: Literal["gas_import"] = "gas_import"
    properties: GasImportProperties
    schedule: dict | None = None


class HeatExportDevice(BaseModel):
    """``DeviceRequest(type="heat_export")`` with typed properties."""

    name: str = Field(..., min_length=1, max_length=100)
    type: Literal["heat_export"] = "heat_export"
    properties: HeatExportProperties
    schedule: dict | None = None


TypedDevice = Annotated[
    Union[
        BatteryDevice,
        HeatAccumulatorDevice,
        CHPDevice,
        HeatDemandDevice,
        ElectricityImportDevice,
        ElectricityExportDevice,
        GasImportDevice,
        HeatExportDevice,
    ],
    Field(discriminator="type"),
]
"""Tagged-union of typed devices, discriminated by ``type``.

A site that uses ``TypedDevice`` instead of the loose ``DeviceRequest`` lets
Pydantic dispatch to the right properties class on parse and gives the IDE
the per-type field set on construction. Both shapes serialise to the same
wire JSON, so callers can mix typed and dict-based devices in the same site.
"""


__all__ = [
    "ANSAbility",
    "Profile",
    "BatteryProperties",
    "HeatAccumulatorProperties",
    "CHPProperties",
    "HeatDemandProperties",
    "ElectricityImportProperties",
    "ElectricityExportProperties",
    "GasImportProperties",
    "HeatExportProperties",
    "BatteryDevice",
    "HeatAccumulatorDevice",
    "CHPDevice",
    "HeatDemandDevice",
    "ElectricityImportDevice",
    "ElectricityExportDevice",
    "GasImportDevice",
    "HeatExportDevice",
    "TypedDevice",
]
