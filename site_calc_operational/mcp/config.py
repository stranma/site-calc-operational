"""Configuration for the operational MCP server, loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Subdirectory inside the user's home/Documents folder used as the default
# data root when ``SITE_CALC_OPERATIONAL_DATA_DIR`` is not set. Picked because
# Claude Desktop (and other MCP hosts on Windows) launch the MCP server with
# CWD pinned to a system path like ``C:\WINDOWS\system32`` -- so falling back
# to ``os.getcwd()`` would stash user data in places they cannot find or
# write to. ``~/Documents/site-calc-data`` matches Windows conventions and
# resolves the same way on macOS/Linux (``~/Documents`` exists on both).
_DEFAULT_DATA_SUBDIR = "site-calc-data"


def get_data_dir() -> str:
    """Return the absolute data directory used for ``save_data_file`` / ``fetch_url``.

    Resolution order:

    1. ``SITE_CALC_OPERATIONAL_DATA_DIR`` env var if set.
    2. ``~/Documents/site-calc-data`` if ``~/Documents`` exists (Windows default).
    3. ``~/site-calc-data`` otherwise (Linux/macOS without a Documents folder).

    The returned path is created on first call so callers can write to it
    immediately. Always returns an absolute string -- never ``None`` -- so the
    SSRF/path-containment guard has a concrete root to validate against.

    :returns: Absolute path to the data directory.
    """
    env = os.environ.get("SITE_CALC_OPERATIONAL_DATA_DIR")
    if env:
        candidate = Path(env).expanduser().resolve()
    else:
        home = Path.home()
        documents = home / "Documents"
        # If Documents doesn't exist, drop straight to ~/site-calc-data --
        # don't try to create Documents itself (that would be intrusive on
        # systems that intentionally lack it).
        base = documents if documents.is_dir() else home
        candidate = (base / _DEFAULT_DATA_SUBDIR).resolve()
    candidate.mkdir(parents=True, exist_ok=True)
    return str(candidate)


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
