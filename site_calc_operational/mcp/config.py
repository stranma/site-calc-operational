"""Configuration for the operational MCP server, loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def get_data_dir() -> str | None:
    """Return the configured data directory, or None.

    The directory is resolved against the ``SITE_CALC_OPERATIONAL_DATA_DIR`` env var.
    A ``None`` return means relative file paths resolve against the current working
    directory.

    :returns: Absolute or relative directory path, or ``None``.
    """
    return os.environ.get("SITE_CALC_OPERATIONAL_DATA_DIR") or None


_DEFAULT_API_URL = "http://localhost:8000"


@dataclass(frozen=True)
class Config:
    """MCP server configuration from environment variables.

    :param api_url: Base URL of the on-prem server (no trailing slash).
    :param api_key: Bearer API key (``op_*`` prefix).
    """

    api_url: str
    api_key: str

    @classmethod
    def from_env(cls) -> Config:
        """Load configuration from environment variables.

        Reads ``SITE_CALC_OPERATIONAL_API_URL`` (default ``http://localhost:8000``)
        and ``SITE_CALC_OPERATIONAL_API_KEY`` (required).

        :returns: Frozen :class:`Config` instance.
        :raises ValueError: If ``SITE_CALC_OPERATIONAL_API_KEY`` is unset or empty.
        """
        api_url = os.environ.get("SITE_CALC_OPERATIONAL_API_URL", _DEFAULT_API_URL).rstrip("/")
        api_key = os.environ.get("SITE_CALC_OPERATIONAL_API_KEY", "")
        if not api_key:
            raise ValueError(
                "SITE_CALC_OPERATIONAL_API_KEY environment variable is required. "
                "Set it to a key minted by `site-calc-op create-user` (starts with 'op_')."
            )
        return cls(api_url=api_url, api_key=api_key)
