"""The site: one device list, typed by ``type``.

A site has at most one device of each type and always an
:class:`ElectricityExport`. Battery-only sites (battery + grid legs) are the
common case; a CHP site adds ``gas_import`` and usually ``heat_export``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from site_calc_operational.models._base import RequestModel, ServiceCode


class ANSAbility(RequestModel):
    """A prequalified ancillary-service ability of a device.

    Power rates are fractions of the device's rated power. A battery that may
    offer its full rating in a service's direction uses
    ``min_device_power_rate=0.0, max_device_power_rate=1.0``.
    """

    service: ServiceCode = Field(description="Which product the device is prequalified for.")
    min_device_power_rate: float = Field(
        ge=-1.0,
        lt=1.0,
        description="Lower end of the offerable window as a fraction of rated power (0.0 for a battery).",
    )
    max_device_power_rate: float = Field(
        gt=0.0,
        le=1.0,
        description="Upper end of the offerable window as a fraction of rated power (1.0 = full rating).",
    )
    soc_reserve_fraction: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description=(
            "Share of the battery's energy capacity held back while the service is committed "
            "(upward: kept charged; downward: kept empty). Ignored for a CHP."
        ),
    )

    @model_validator(mode="after")
    def _window(self) -> ANSAbility:
        if self.min_device_power_rate >= self.max_device_power_rate:
            raise ValueError("min_device_power_rate must be below max_device_power_rate")
        return self


class Battery(RequestModel):
    """Electricity storage.

    ``initial_soc_mwh`` is the state of charge at the start of the delivery
    day (local midnight). In a daily loop pass the previous day's
    ``soc_end_mwh`` from :class:`~site_calc_operational.models.DayAheadPlan`.
    """

    type: Literal["battery"] = "battery"
    name: str = Field(min_length=1, max_length=100, description="Unique device name within the site.")
    capacity_mwh: float = Field(gt=0, description="Usable energy capacity in MWh.")
    max_power_mw: float = Field(gt=0, description="Rated charge and discharge power in MW.")
    efficiency: float = Field(gt=0, le=1, description="Round-trip efficiency, e.g. 0.9.")
    initial_soc_mwh: float = Field(ge=0, description="State of charge at local midnight of the delivery day, MWh.")
    ans_abilities: list[ANSAbility] = Field(
        default_factory=list,
        description="Ancillary services the battery may be reserved for; empty means day-ahead only.",
    )

    @model_validator(mode="after")
    def _soc_within_capacity(self) -> Battery:
        if self.initial_soc_mwh > self.capacity_mwh:
            raise ValueError("initial_soc_mwh must not exceed capacity_mwh")
        return self


class CHP(RequestModel):
    """Combined heat and power unit; flows are the rated values while running."""

    type: Literal["chp"] = "chp"
    name: str = Field(min_length=1, max_length=100, description="Unique device name within the site.")
    gas_input_mw: float = Field(gt=0, description="Gas consumption at rated output, MW.")
    el_output_mw: float = Field(gt=0, description="Electrical output at rated load, MW.")
    heat_output_mw: float = Field(ge=0, description="Heat output at rated load, MW (0 for power-only).")
    is_binary: bool = Field(default=False, description="True when the unit runs either off or at rated load.")
    max_starts_per_day: int | None = Field(default=None, ge=0, description="Cap on starts within the delivery day.")
    min_continuous_run_hours: float | None = Field(
        default=None, ge=0, description="Minimum uninterrupted run once started, hours."
    )
    ans_abilities: list[ANSAbility] = Field(
        default_factory=list, description="Ancillary services the unit may be reserved for."
    )


class GasImport(RequestModel):
    """Gas supply at a flat tariff. Required when the site has a CHP."""

    type: Literal["gas_import"] = "gas_import"
    name: str = Field(min_length=1, max_length=100, description="Unique device name within the site.")
    price_eur_per_mwh: float = Field(ge=0, description="Gas price, EUR per MWh of gas.")
    max_import_mw: float | None = Field(
        default=None, gt=0, description="Supply limit in MW; also caps the day's total gas energy."
    )


class HeatExport(RequestModel):
    """District-heat sale at a flat price."""

    type: Literal["heat_export"] = "heat_export"
    name: str = Field(min_length=1, max_length=100, description="Unique device name within the site.")
    price_eur_per_mwh: float = Field(description="Heat sale price, EUR per MWh of heat.")
    max_export_mw: float | None = Field(default=None, gt=0, description="Heat offtake limit, MW.")
    max_export_total_mwh: float | None = Field(default=None, gt=0, description="Heat offtake limit for the day, MWh.")


class ElectricityImport(RequestModel):
    """Grid buy leg. Omit it for an export-only site."""

    type: Literal["electricity_import"] = "electricity_import"
    name: str = Field(min_length=1, max_length=100, description="Unique device name within the site.")
    max_import_mw: float | None = Field(default=None, gt=0, description="Connection import limit, MW.")
    buy_fee_eur_per_mwh: float = Field(
        default=0.0,
        ge=0,
        description="Fee added to the day-ahead price when planning purchases; settlement stays at market price.",
    )


class ElectricityExport(RequestModel):
    """Grid sell leg. Every site has one."""

    type: Literal["electricity_export"] = "electricity_export"
    name: str = Field(min_length=1, max_length=100, description="Unique device name within the site.")
    max_export_mw: float | None = Field(default=None, gt=0, description="Connection export limit, MW.")
    sell_fee_eur_per_mwh: float = Field(
        default=0.0,
        ge=0,
        description="Fee subtracted from the day-ahead price when planning sales; settlement stays at market price.",
    )


Device = Annotated[
    Battery | CHP | GasImport | HeatExport | ElectricityImport | ElectricityExport,
    Field(discriminator="type"),
]
"""Any device the site may contain; the ``type`` field selects the model."""


class Site(RequestModel):
    """One site to plan.

    Rules the server enforces (and this model checks first): at most one
    device per type, unique names, an ``electricity_export`` always, a
    ``gas_import`` whenever there is a ``chp``, and ``ans_abilities`` on at
    most one device.
    """

    site_id: str = Field(min_length=1, max_length=100, description="Your identifier for the site; echoed in runs.")
    devices: list[Device] = Field(min_length=1, description="The site's devices, one per type.")

    @model_validator(mode="after")
    def _shape(self) -> Site:
        kinds = [d.type for d in self.devices]
        dupes = sorted({k for k in kinds if kinds.count(k) > 1})
        if dupes:
            raise ValueError(f"at most one device per type; duplicated: {dupes}")
        names = [d.name for d in self.devices]
        if len(set(names)) != len(names):
            raise ValueError("device names must be unique")
        if "electricity_export" not in kinds:
            raise ValueError("site needs an electricity_export device")
        if "chp" in kinds and "gas_import" not in kinds:
            raise ValueError("a chp needs a gas_import device")
        with_abilities = [d.name for d in self.devices if getattr(d, "ans_abilities", None)]
        if len(with_abilities) > 1:
            raise ValueError(f"only one device may declare ans_abilities, got {with_abilities}")
        return self

    @property
    def battery(self) -> Battery | None:
        """The site's battery, if any."""
        return next((d for d in self.devices if isinstance(d, Battery)), None)

    def declared_services(self) -> set[str]:
        """Service codes some device declares an ability for."""
        return {a.service for d in self.devices for a in getattr(d, "ans_abilities", [])}
