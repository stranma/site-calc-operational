"""Integration test against a live server-onprem instance.

Skipped unless ``ONPREM_INTEGRATION=1`` is set in the environment.  Requires
``server-onprem`` to be running (e.g. via docker compose) and the following
environment variables to be provided by the operator:

- ``ONPREM_BASE_URL`` -- base URL of the running server (default: ``http://localhost:8000``)
- ``ONPREM_API_KEY``  -- plaintext ``op_...`` token created with ``site-calc-op create-user``
- ``ONPREM_FIXTURE``  -- path to a JSON request fixture
  (default: ``../server-onprem/tests/fixtures/site_chp_battery.json``)

This test is intentionally excluded from the normal ``uv run pytest tests/`` run.
The operator runs it manually to validate a deployed instance:

.. code-block:: bash

    ONPREM_INTEGRATION=1 ONPREM_API_KEY="op_..." uv run pytest tests/test_onprem_integration.py -v
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from site_calc_operational.api.onprem_client import OnPremClient

_INTEGRATION = pytest.mark.skipif(
    os.environ.get("ONPREM_INTEGRATION") != "1",
    reason="needs running server-onprem (set ONPREM_INTEGRATION=1)",
)


@_INTEGRATION
def test_real_solve() -> None:
    """Failure mode: the client cannot complete a round-trip against a real server, or
    the response body is missing the top-level ``summary`` key expected from a successful
    device-planning solve."""
    base_url = os.environ.get("ONPREM_BASE_URL", "http://localhost:8000")
    api_key = os.environ["ONPREM_API_KEY"]
    fixture_path = Path(os.environ.get("ONPREM_FIXTURE", "../server-onprem/tests/fixtures/site_chp_battery.json"))
    payload = json.loads(fixture_path.read_text())
    with OnPremClient(base_url=base_url, api_key=api_key) as c:
        out = c.device_planning(payload)
    assert "summary" in out
