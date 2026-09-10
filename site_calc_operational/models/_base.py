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

    model_config = ConfigDict(extra="forbid")


class ResponseModel(BaseModel):
    """Base for everything received from the server: unknown fields are kept, not rejected."""

    model_config = ConfigDict(extra="allow")
