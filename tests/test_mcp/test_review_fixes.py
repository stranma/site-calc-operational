"""Regression tests for the four reviewer findings on PR #1.

Each test pins one specific failure mode the reviewer flagged.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import respx
from httpx import Response

from site_calc_operational.api.onprem_client import BackoffPolicy, OnPremClient
from site_calc_operational.api.onprem_exceptions import BusyError
from site_calc_operational.mcp import server as srv
from site_calc_operational.mcp.data_loaders import (
    _resolve_outpath,
    fetch_url_to_file,
    save_csv,
)

# ---------------------------------------------------------------------------
# F1: Path containment -- save_data_file / fetch_url cannot escape data_dir
# ---------------------------------------------------------------------------


def test_resolve_outpath_rejects_absolute_outside_data_dir(tmp_path: Path) -> None:
    """Failure mode (review F1): absolute paths outside the data dir are
    accepted, so save_data_file('/etc/cron.d/evil', ...) silently writes the
    file."""
    outside = tmp_path.parent / "outside.csv"
    with pytest.raises(ValueError, match="outside the data directory"):
        _resolve_outpath(str(outside), data_dir=str(tmp_path), default_ext=".csv")


def test_resolve_outpath_rejects_dotdot_traversal(tmp_path: Path) -> None:
    """Failure mode (review F1): ../../etc/passwd traversal escapes the
    sandbox because os.path.join honors the leading slash."""
    with pytest.raises(ValueError, match="outside the data directory"):
        _resolve_outpath("../../sneaky.csv", data_dir=str(tmp_path), default_ext=".csv")


def test_resolve_outpath_allows_inside_relative(tmp_path: Path) -> None:
    """Failure mode (review F1): the containment check is too aggressive and
    blocks legitimate relative paths under the data dir."""
    out = _resolve_outpath("sub/prices.csv", data_dir=str(tmp_path), default_ext=".csv")
    assert os.path.normcase(out).startswith(os.path.normcase(str(tmp_path)))


def test_resolve_outpath_allows_absolute_inside_data_dir(tmp_path: Path) -> None:
    """Failure mode: absolute paths *inside* the data dir are wrongly rejected,
    breaking workflows that pre-compute the absolute path."""
    inside = tmp_path / "ok.csv"
    out = _resolve_outpath(str(inside), data_dir=str(tmp_path), default_ext=".csv")
    assert out == os.path.normpath(str(inside))


def test_save_csv_rejects_absolute_outside_data_dir(tmp_path: Path) -> None:
    """Failure mode (review F1): save_csv writes to /tmp/foo.csv when data_dir
    is /home/user/data, leaking files outside the sandbox."""
    with pytest.raises(ValueError, match="outside the data directory"):
        save_csv(
            file_path=str(tmp_path.parent / "leak.csv"),
            columns={"x": [1.0]},
            data_dir=str(tmp_path),
            overwrite=False,
        )


# ---------------------------------------------------------------------------
# F2: SSRF -- fetch_url blocks private/loopback hosts
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_url_blocks_localhost(tmp_path: Path) -> None:
    """Failure mode (review F2): http://localhost/admin slips through and
    exposes internal services to a prompt-injected LLM."""
    with pytest.raises(ValueError, match="disallowed host|non-public"):
        fetch_url_to_file(
            url="http://localhost/admin",
            data_dir=str(tmp_path),
            file_path=None,
            overwrite=False,
        )


@respx.mock
def test_fetch_url_blocks_loopback_ip(tmp_path: Path) -> None:
    """Failure mode (review F2): bare 127.0.0.1 URLs bypass any hostname check."""
    with pytest.raises(ValueError, match="non-public"):
        fetch_url_to_file(
            url="http://127.0.0.1:8080/data.csv",
            data_dir=str(tmp_path),
            file_path=None,
            overwrite=False,
        )


@respx.mock
def test_fetch_url_blocks_link_local_metadata(tmp_path: Path) -> None:
    """Failure mode (review F2): the AWS instance metadata endpoint
    (169.254.169.254) is reachable from the user's machine and would leak
    cloud credentials if a prompt-injected LLM hits it."""
    with pytest.raises(ValueError, match="non-public"):
        fetch_url_to_file(
            url="http://169.254.169.254/latest/meta-data/",
            data_dir=str(tmp_path),
            file_path=None,
            overwrite=False,
        )


@respx.mock
def test_fetch_url_blocks_rfc1918_private(tmp_path: Path) -> None:
    """Failure mode (review F2): RFC 1918 ranges (10/8, 192.168/16) are
    reachable from the user's intranet and must not be fetchable."""
    with pytest.raises(ValueError, match="non-public"):
        fetch_url_to_file(
            url="http://192.168.1.1/router-config",
            data_dir=str(tmp_path),
            file_path=None,
            overwrite=False,
        )


# ---------------------------------------------------------------------------
# F3: busy_retry=None must actually disable retries
# ---------------------------------------------------------------------------


@respx.mock
def test_busy_retry_none_does_not_retry() -> None:
    """Failure mode (review F3): docstring says None disables retries, but the
    constructor replaced None with BackoffPolicy() so callers still saw retries."""
    route = respx.post("http://stub/v1/device-planning").mock(
        return_value=Response(503, json={"error": {"code": "BUSY", "message": "busy"}})
    )
    client = OnPremClient(base_url="http://stub", api_key="op_x", busy_retry=None)
    with pytest.raises(BusyError):
        client.device_planning({})
    assert route.call_count == 1, (
        f"Expected exactly 1 request when busy_retry=None, got {route.call_count}. "
        "This means 'None disables retries' is still a lie."
    )


@respx.mock
def test_busy_retry_default_still_retries() -> None:
    """Failure mode: the F3 fix accidentally disabled retries for the default
    constructor, so production callers no longer ride out a transient 503."""
    route = respx.post("http://stub/v1/device-planning").mock(
        return_value=Response(503, json={"error": {"code": "BUSY", "message": "busy"}})
    )
    # Tight policy keeps the test fast.
    client = OnPremClient(
        base_url="http://stub",
        api_key="op_x",
        busy_retry=BackoffPolicy(max_retries=2, initial_delay_seconds=0.01, max_delay_seconds=0.01),
    )
    with pytest.raises(BusyError):
        client.device_planning({})
    assert route.call_count == 3, f"Expected 1 initial + 2 retries = 3 requests, got {route.call_count}."


# ---------------------------------------------------------------------------
# F4: solve() reports replay status from X-Idempotent-Replay header
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_module_state() -> None:
    """Each test gets a fresh module store and an injected client."""
    srv._reset_store_for_tests()
    client = OnPremClient(base_url="http://stub", api_key="op_test", busy_retry=None)
    srv._set_client_for_tests(client)
    yield
    srv._set_client_for_tests(None)
    srv._reset_store_for_tests()


def _build_solveable_scenario() -> str:
    sid = srv.create_scenario(name="x", site_id="s")["scenario_id"]
    srv.set_timespan(sid, "2026-01-15T00:00:00+00:00", "2026-01-16T00:00:00+00:00", "1h")
    srv.add_device(sid, "Bat", "battery", {"capacity": 6.0, "max_power": 3.0, "efficiency": 0.9})
    srv.add_device(sid, "Imp", "electricity_import", {"max_import": 10.0, "price": 50.0})
    srv.add_device(sid, "HD", "heat_demand", {"demand_profile": [2.0] * 24})
    return sid


@respx.mock
def test_solve_reports_replay_false_on_fresh_solve() -> None:
    """Failure mode (review F4): solve() always reports replay=False,
    so an LLM cannot tell whether its idempotency key actually short-circuited."""
    respx.post("http://stub/v1/device-planning").mock(
        return_value=Response(
            200,
            json={"run_id": "rid-1", "summary": {"solver_status": "Optimal", "expected_profit": 1.0}},
        )
    )
    sid = _build_solveable_scenario()
    out = srv.solve(sid, idempotency_key="key-1")
    assert out["replay"] is False, "First solve must report replay=False"


@respx.mock
def test_solve_reports_replay_true_when_header_present() -> None:
    """Failure mode (review F4): the X-Idempotent-Replay header is dropped on
    the floor, so the LLM doesn't know the cached response was returned."""
    respx.post("http://stub/v1/device-planning").mock(
        return_value=Response(
            200,
            json={"run_id": "rid-1", "summary": {"solver_status": "Optimal", "expected_profit": 1.0}},
            headers={"X-Idempotent-Replay": "true"},
        )
    )
    sid = _build_solveable_scenario()
    out = srv.solve(sid, idempotency_key="key-1")
    assert out["replay"] is True, "Header X-Idempotent-Replay=true must surface as replay=True"


# ---------------------------------------------------------------------------
# Code-aware from_response for INFEASIBLE / UNBOUNDED on HTTP 422
# ---------------------------------------------------------------------------


def test_from_response_maps_infeasible_code_over_status() -> None:
    """Failure mode: 422 INFEASIBLE is silently classified as ValidationError
    (the default for 422), so callers cannot distinguish a schema-level reject
    from a modeling error that the LP solver caught."""
    from site_calc_operational.api.onprem_exceptions import (
        InfeasibleScenarioError,
        ValidationError,
        from_response,
    )

    body = {"error": {"code": "INFEASIBLE", "message": "no feasible assignment", "details": {"hint": "X"}}}
    exc = from_response(422, body)
    assert isinstance(exc, InfeasibleScenarioError)
    assert not isinstance(exc, ValidationError)
    assert exc.code == "INFEASIBLE"
    assert exc.details == {"hint": "X"}


def test_from_response_maps_unbounded_code_over_status() -> None:
    """Failure mode: 422 UNBOUNDED falls through to ValidationError, so an
    LLM cannot specifically catch the unbounded case to suggest adding a bound."""
    from site_calc_operational.api.onprem_exceptions import UnboundedScenarioError, from_response

    body = {"error": {"code": "UNBOUNDED", "message": "objective is unbounded"}}
    exc = from_response(422, body)
    assert isinstance(exc, UnboundedScenarioError)


def test_from_response_falls_back_to_status_for_generic_422() -> None:
    """Failure mode: code-based mapping accidentally swallows the existing
    422->ValidationError default for schema-level rejects."""
    from site_calc_operational.api.onprem_exceptions import ValidationError, from_response

    body = {"error": {"code": "VALIDATION_ERROR", "message": "field 'name' is required"}}
    exc = from_response(422, body)
    assert isinstance(exc, ValidationError)


@respx.mock
def test_solve_raises_infeasible_scenario_error_on_422() -> None:
    """Failure mode: the MCP solve() tool returns a generic OnPremError on
    infeasibility, so the LLM driving it cannot catch the specific class."""
    from site_calc_operational.api.onprem_exceptions import InfeasibleScenarioError

    respx.post("http://stub/v1/device-planning").mock(
        return_value=Response(
            422,
            json={
                "error": {
                    "code": "INFEASIBLE",
                    "message": "demand exceeds supply",
                    "details": {"hint": "add a market import"},
                }
            },
        )
    )
    sid = _build_solveable_scenario()
    with pytest.raises(InfeasibleScenarioError) as excinfo:
        srv.solve(sid)
    assert excinfo.value.code == "INFEASIBLE"
    assert "demand exceeds supply" in excinfo.value.message


@respx.mock
def test_solve_materialises_debug_lp_to_data_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure mode: the server returns a base64 LP blob in details, but the
    MCP layer leaves it as base64 -- the LLM cannot use it and the user has
    nowhere to point a debugger.
    """
    import base64 as _b64

    from site_calc_operational.api.onprem_exceptions import InfeasibleScenarioError

    monkeypatch.setenv("SITE_CALC_OPERATIONAL_DATA_DIR", str(tmp_path))

    fake_lp = b"\\Problem name: synthetic\\\nMinimize\n obj: x\nEnd\n"
    respx.post("http://stub/v1/device-planning").mock(
        return_value=Response(
            422,
            json={
                "error": {
                    "code": "INFEASIBLE",
                    "message": "synthetic",
                    "details": {
                        "hint": "add a buffer",
                        "debug_lp_filename": "debug_problem.lp",
                        "debug_lp_size_bytes": len(fake_lp),
                        "debug_lp_b64": _b64.b64encode(fake_lp).decode("ascii"),
                    },
                }
            },
        )
    )
    sid = _build_solveable_scenario()
    with pytest.raises(InfeasibleScenarioError) as excinfo:
        srv.solve(sid)

    details = excinfo.value.details
    assert details is not None
    # The base64 blob is gone; an absolute path takes its place.
    assert "debug_lp_b64" not in details
    assert "debug_lp_path" in details
    saved_path = Path(details["debug_lp_path"])
    assert saved_path.exists()
    assert saved_path.read_bytes() == fake_lp
    # File must be inside the data dir, not anywhere else.
    assert os.path.normcase(str(saved_path)).startswith(os.path.normcase(str(tmp_path)))


@respx.mock
def test_solve_handles_truncated_debug_lp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Failure mode: the truncation marker for an oversized LP gets stripped
    on the way through the SDK, so the LLM sees neither path nor warning."""
    from site_calc_operational.api.onprem_exceptions import InfeasibleScenarioError

    monkeypatch.setenv("SITE_CALC_OPERATIONAL_DATA_DIR", str(tmp_path))

    respx.post("http://stub/v1/device-planning").mock(
        return_value=Response(
            422,
            json={
                "error": {
                    "code": "INFEASIBLE",
                    "message": "huge LP",
                    "details": {
                        "hint": "x",
                        "debug_lp_filename": "debug_problem.lp",
                        "debug_lp_size_bytes": 50_000_000,
                        "debug_lp_truncated": True,
                    },
                }
            },
        )
    )
    sid = _build_solveable_scenario()
    with pytest.raises(InfeasibleScenarioError) as excinfo:
        srv.solve(sid)
    details = excinfo.value.details
    assert details is not None
    assert details.get("debug_lp_truncated") is True
    assert "debug_lp_path" not in details  # nothing to materialise


@respx.mock
def test_solve_does_not_double_record_replayed_run() -> None:
    """Failure mode: solve() records the same run_id twice when the server
    replays it, inflating run_count in list_scenarios."""
    respx.post("http://stub/v1/device-planning").mock(
        return_value=Response(
            200,
            json={"run_id": "rid-replay", "summary": {"solver_status": "Optimal", "expected_profit": 0.0}},
            headers={"X-Idempotent-Replay": "true"},
        )
    )
    sid = _build_solveable_scenario()
    srv.solve(sid, idempotency_key="dup")
    srv.solve(sid, idempotency_key="dup")  # would normally double-append run_id
    listed = srv.list_scenarios()
    matches = [s for s in listed if s["scenario_id"] == sid]
    assert matches and matches[0]["run_count"] == 1, (
        f"Replayed runs must not be counted twice; run_count={matches[0]['run_count']}"
    )
