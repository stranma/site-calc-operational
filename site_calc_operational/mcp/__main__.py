"""Allow ``python -m site_calc_operational.mcp`` to launch the FastMCP server."""

from __future__ import annotations

from site_calc_operational.mcp.server import main

if __name__ == "__main__":
    main()
