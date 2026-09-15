"""Shared building blocks for the wire models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

ServiceCode = Literal["afrr_plus", "afrr_minus", "mfrr_plus", "mfrr_minus"]
"""Ancillary-service product codes the server understands.

``afrr_plus`` / ``afrr_minus`` are automatic frequency restoration reserve in
the upward (deliver power) and downward (absorb power) direction;
``mfrr_plus`` / ``mfrr_minus`` are the manual reserve equivalents.
"""

BLOCKS_PER_DAY = 6
"""Reservation is contracted per 4-hour block; block ``b`` covers local hours ``4b`` to ``4b + 4``."""

QUARTER_HOURS_PER_DAY = 96
"""Day-ahead prices and bids are per quarter-hour of a regular (non-DST) day."""


class RequestModel(BaseModel):
    """Base for everything sent to the server: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def check_battery_has_next_day_prices(site: object, day: object) -> None:
    """Raise ``ValueError`` when a battery site is missing the D+1 prices (mirrors the server's rule)."""
    if getattr(site, "has_ler", False) and getattr(day, "da_price_eur_per_mwh_d1", None) is None:
        raise ValueError(
            "a LER site needs day.da_price_eur_per_mwh_d1: the planner values the midnight state "
            "of charge against the next day's prices"
        )


class ResponseModel(BaseModel):
    """Base for everything received from the server: unknown fields are kept, not rejected."""

    model_config = ConfigDict(extra="allow")
