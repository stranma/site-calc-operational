"""The site: one device list, typed by ``type``.

A Site may repeat device types; each device has a unique name. One device
may declare ANS abilities. Any storage device extends planning to D+1.
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
    """Gas supply at a flat tariff."""

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
    """Optional grid sell leg."""

    type: Literal["electricity_export"] = "electricity_export"
    name: str = Field(min_length=1, max_length=100, description="Unique device name within the site.")
    max_export_mw: float | None = Field(default=None, gt=0, description="Connection export limit, MW.")
    sell_fee_eur_per_mwh: float = Field(
        default=0.0,
        ge=0,
        description="Fee subtracted from the day-ahead price when planning sales; settlement stays at market price.",
    )

    exclusive_with: str | None = Field(
        default=None, description="Name of an electricity import; prevents simultaneous buying and selling."
    )


class Storage(RequestModel):
    """A limited-energy resource, including thermal or other material storage.

    All profiles on its Site cover D and D+1. Energy is in MWh and power in MW.
    """

    type: Literal["storage", "heat_accumulator"] = "storage"
    name: str = Field(min_length=1, max_length=100)
    material: Literal["electricity", "heat", "gas", "hydrogen", "cooling", "steam"] = "heat"
    capacity_mwh: float = Field(gt=0)
    max_power_mw: float = Field(gt=0)
    efficiency: float = Field(gt=0, le=1)
    initial_soc_mwh: float = Field(ge=0)
    loss_rate: float = Field(default=0.0, ge=0, lt=1)
    charge_efficiency: float | None = Field(default=None, gt=0, le=1)
    discharge_efficiency: float | None = Field(default=None, gt=0, le=1)
    ans_abilities: list[ANSAbility] = Field(default_factory=list)

    @model_validator(mode="after")
    def _valid_storage(self) -> Storage:
        if self.initial_soc_mwh > self.capacity_mwh:
            raise ValueError("initial_soc_mwh must not exceed capacity_mwh")
        if self.type == "heat_accumulator" and self.material != "heat":
            raise ValueError("heat_accumulator stores heat")
        if self.ans_abilities and self.material != "electricity":
            raise ValueError("only electrical storage can declare electrical ANS abilities")
        return self


class Profile(RequestModel):
    """A fixed or controllable load/supply, with profiles covering the solved horizon.

    Equal minimum and maximum describes fixed demand/production. Otherwise
    the optimizer selects values between them. Prices are EUR/MWh.
    """

    type: Literal[
        "electricity_demand", "heat_demand", "generic_demand", "electricity_supply", "generic_supply", "photovoltaic"
    ]
    name: str = Field(min_length=1, max_length=100)
    material: Literal["electricity", "heat", "gas", "hydrogen", "cooling", "steam"] = "electricity"
    maximum_mw: list[float] = Field(min_length=1)
    minimum_mw: list[float] | float = 0.0
    price_eur_per_mwh: list[float] | None = None
    min_total_mwh: float | None = Field(default=None, ge=0)
    max_total_mwh: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _valid_profile(self) -> Profile:
        expected: Literal["heat", "electricity"] = "heat" if self.type == "heat_demand" else "electricity"
        if self.type not in ("generic_demand", "generic_supply"):
            if "material" in self.model_fields_set and self.material != expected:
                raise ValueError(f"{self.type} requires material {expected}")
            self.material = expected
        if self.min_total_mwh is not None and self.max_total_mwh is not None:
            if self.min_total_mwh > self.max_total_mwh:
                raise ValueError("min_total_mwh must not exceed max_total_mwh")
        if any(v < 0 for v in self.maximum_mw):
            raise ValueError("maximum_mw must be nonnegative")
        minimum = self.minimum_mw if isinstance(self.minimum_mw, list) else [self.minimum_mw] * len(self.maximum_mw)
        if len(minimum) != len(self.maximum_mw) or any(
            lo < 0 or lo > hi for lo, hi in zip(minimum, self.maximum_mw, strict=True)
        ):
            raise ValueError("minimum_mw must match and lie within maximum_mw")
        if self.price_eur_per_mwh is not None and len(self.price_eur_per_mwh) != len(self.maximum_mw):
            raise ValueError("price profile must match maximum_mw")
        return self


class Market(RequestModel):
    """A material market. Electrical market prices must match the supplied day prices."""

    type: Literal["import_market", "export_market"]
    name: str = Field(min_length=1, max_length=100)
    material: Literal["electricity", "heat", "gas", "hydrogen", "cooling", "steam"]
    price_eur_per_mwh: list[float] = Field(min_length=1)
    max_flow_mw: float = Field(gt=0)
    max_total_mwh: float | None = Field(default=None, ge=0)


class Composite(RequestModel):
    """Mutually exclusive operating pieces of one device, following core composite rules."""

    type: Literal["composite"] = "composite"
    name: str = Field(min_length=1, max_length=100)
    sub_devices: list[CHP | Profile] = Field(min_length=2)
    force_switch: bool = False
    start: int | str | None = None

    @model_validator(mode="after")
    def _pieces(self) -> Composite:
        if any(getattr(d, "ans_abilities", None) for d in self.sub_devices):
            raise ValueError("core composite pieces cannot declare ANS abilities")
        for d in self.sub_devices:
            if any(
                getattr(d, name, None) is not None
                for name in ("min_total_mwh", "max_total_mwh", "max_starts_per_day", "min_continuous_run_hours")
            ):
                raise ValueError("composite pieces cannot carry cumulative or temporal constraints")
        return self


Device = Annotated[
    Market
    | Composite
    | Battery
    | CHP
    | GasImport
    | HeatExport
    | ElectricityImport
    | ElectricityExport
    | Storage
    | Profile,
    Field(discriminator="type"),
]
"""Any device the site may contain; the ``type`` field selects the model."""


class Site(RequestModel):
    """One site to plan.

    Names must be unique, and at most one device may declare ANS abilities.
    Physical material balances decide which supplies and markets are needed.
    """

    site_id: str = Field(min_length=1, max_length=100, description="Your identifier for the site; echoed in runs.")
    devices: list[Device] = Field(min_length=1, description="The site's devices, with unique names.")

    @model_validator(mode="after")
    def _shape(self) -> Site:
        names = [d.name for d in self.devices]
        if len(set(names)) != len(names):
            raise ValueError("device names must be unique")
        with_abilities = [d.name for d in self.devices if getattr(d, "ans_abilities", None)]
        if len(with_abilities) > 1:
            raise ValueError(f"only one device may declare ans_abilities, got {with_abilities}")
        return self

    @property
    def battery(self) -> Battery | None:
        """The site's battery, if any."""
        return next((d for d in self.devices if isinstance(d, Battery)), None)

    @property
    def has_ler(self) -> bool:
        """Whether any stored-energy device requires a two-day solve."""
        return any(isinstance(d, (Battery, Storage)) for d in self.devices)

    def declared_services(self) -> set[str]:
        """Service codes some device declares an ability for."""
        return {a.service for d in self.devices for a in getattr(d, "ans_abilities", [])}
