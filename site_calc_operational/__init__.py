"""Site-Calc Operational Client.

Python client for day-ahead bidding and short-term dispatch optimization with
ancillary services. Two top-level clients are exposed:

- :class:`OperationalClient` -- async client for the SaaS REST API.
- :class:`OnPremClient` -- sync client for self-hosted ``server-onprem``
  deployments. Companion :class:`BackoffPolicy` and :class:`HealthInfo`
  dataclasses are also exported.

An optional MCP server (extra: ``site-calc-operational[mcp]``) wraps the
on-prem client for LLM-driven scenario assembly.
"""

from site_calc_operational.api.client import OperationalClient
from site_calc_operational.api.onprem_client import (
    BackoffPolicy,
    HealthInfo,
    OnPremClient,
)

__version__ = "0.3.0"

__all__ = [
    "BackoffPolicy",
    "HealthInfo",
    "OnPremClient",
    "OperationalClient",
    "__version__",
]
