"""Shared base types used by both ``reservation_bids`` and ``devices`` modules.

This module exists to break the ``devices.py -> reservation_bids.py`` import
cycle that would otherwise prevent ``SiteRequest.devices`` from referencing
the ``TypedDevice`` union from ``devices.py``. Anything imported by *both*
sibling modules belongs here.
"""

from __future__ import annotations

from typing import Literal

# Lowercase wire codes; mirror ``site_calc.domain.ans.AncillaryService.code``.
ServiceCode = Literal["afrr_plus", "afrr_minus", "mfrr_plus", "mfrr_minus"]
