"""C4: optimal_bidding() must surface the 501 envelope as NotImplementedOnServer."""

from __future__ import annotations

import pytest
import respx
from httpx import Response

from site_calc_operational.api.onprem_client import OnPremClient
from site_calc_operational.api.onprem_exceptions import NotImplementedOnServer


@respx.mock
def test_optimal_bidding_501_maps_to_typed_error() -> None:
    """Failure mode: server returns 501 with NOT_IMPLEMENTED envelope but the SDK
    raises a generic OnPremError (or ServerError), so callers cannot pattern-match
    on `except NotImplementedOnServer:` to gracefully degrade. Catches both wrong
    exception type AND dropping the envelope's `code` and `tracking` fields."""
    respx.post("http://stub/v1/optimal-bidding").mock(
        return_value=Response(
            501,
            json={
                "error": {
                    "code": "NOT_IMPLEMENTED",
                    "message": "Optimal bidding is not yet supported.",
                    "tracking": "https://linear.app/issues/some-future",
                }
            },
        )
    )
    c = OnPremClient(base_url="http://stub", api_key="op_x")
    with pytest.raises(NotImplementedOnServer) as excinfo:
        c.optimal_bidding({})
    assert type(excinfo.value) is NotImplementedOnServer  # exact type, not subclass match
    assert excinfo.value.code == "NOT_IMPLEMENTED"
    assert excinfo.value.tracking == "https://linear.app/issues/some-future"
    assert excinfo.value.http_status == 501
