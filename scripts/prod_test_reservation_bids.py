"""SDK-level acceptance test for the on-prem reservation-bid endpoints.

Exercises the public Python surface against a live on-prem deployment.
Every server call goes through
:class:`site_calc_operational.api.onprem_client.OnPremClient` and every
payload is built from the typed Pydantic models in
:mod:`site_calc_operational.models` -- so the script also doubles as a
worked example of the supported SDK surface. If anything here needs to
drop back to a raw dict, that is a gap in the SDK and should be fixed
upstream rather than bypassed here.

What it checks (against a live, authenticated on-prem server):

  [1/8] ``GET /v1/health`` returns ``status='ok'`` and ``db_ok=True``
  [2/8] ``POST /v1/reservation-bids`` returns 1..6 bids, positive revenue,
        ``diagnostics.winner_is_maximal=True``, and a
        ``most_probable_realization`` consistent with the bundled
        ``evaluation.expected_revenue``
  [3/8] Idempotency replay -- the same ``Idempotency-Key`` returns the
        same body and the server sets ``X-Idempotent-Replay: true``
  [4/8] ``POST /v1/reservation-bids/evaluate`` on the planner's own bids
        reproduces the planner's expected revenue bit-exactly
  [5/8] ``POST /v1/reservation-bids/most-probable-realization`` standalone
        matches the planner's bundled MPR field-by-field
  [6/8] CHP temporal constraints (``must_run``, ``must_be_idle``,
        ``min_continuous_run_hours``) round-trip end-to-end: forcing the
        unit idle during the evening peak materially lowers the planner's
        expected revenue vs the unconstrained baseline.
  [7/8] 422 ``TRANSLATION_ERROR`` when ``ans_abilities`` is stripped
        (surfaces as :class:`ValidationError`)
  [8/8] 401 with a bogus bearer (surfaces as
        :class:`AuthenticationError`)

The 422 path is a server validation rejection -- no solve is triggered.
The 401 path never reaches the solver.

Credentials are loaded (in priority order) from:

  1. environment variables ``ONPREM_API_KEY`` (required) and
     ``ONPREM_BASE_URL`` (default ``https://operational.algoenergy.cz``)
  2. an ``.env``-style file pointed to by ``--env-file`` or by the
     ``ONPREM_ENV_FILE`` environment variable

Usage::

    cd client-operational
    export ONPREM_API_KEY=op_...
    uv run python scripts/prod_test_reservation_bids.py

Exits non-zero on any failure. This is an acceptance check, not a load
test -- don't loop it.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running the script before ``uv sync`` has been run against this branch
# by surfacing the import error verbatim with hint.
try:
    from site_calc_operational.api.onprem_client import OnPremClient
    from site_calc_operational.api.onprem_exceptions import (
        AuthenticationError,
        OnPremError,
        ValidationError,
    )
    from site_calc_operational.models import (
        ANSAbility,
        BidAcceptanceEntry,
        CHPDevice,
        CHPProperties,
        ElectricityExportDevice,
        ElectricityExportProperties,
        GasImportDevice,
        GasImportProperties,
        HeatExportDevice,
        HeatExportProperties,
        LogNormalParams,
        ReservationBidEvaluateRequest,
        ReservationBidIn,
        ReservationBidMPRRequest,
        ReservationBidPlanRequest,
        ReservationBidPlanResult,
        SiteRequest,
        TimeSpanRequest,
        four_hour_block_starts,
    )
except ImportError as exc:  # pragma: no cover - bootstrap aid
    print(
        f"[FAIL] site_calc_operational not importable: {exc}\n"
        "       run `uv sync --extra dev` in client-operational/ first",
        file=sys.stderr,
    )
    sys.exit(2)


PRAGUE_UTC_OFFSET = timezone(timedelta(hours=2), name="Europe/Prague-summer")
TARGET_DATE = datetime(2026, 5, 13, 0, 0, tzinfo=PRAGUE_UTC_OFFSET)


# 96 quarter-hour synthetic day-ahead clearing prices (EUR/MWh) for the
# target date: morning ramp, midday dip, evening peak. Enough shape that
# the LP has a real choice of when to run.
_DA_PROFILE_24H_15MIN: list[float] = (
    [105.0] * 16 + [120.0] * 16 + [130.0] * 16 + [115.0] * 16 + [180.0] * 16 + [140.0] * 16
)
assert len(_DA_PROFILE_24H_15MIN) == 96

_AFRR_PLUS_PARAMS = [(1.5, 0.6), (2.0, 0.6), (1.0, 0.6), (1.8, 0.6), (3.0, 0.6), (2.3, 0.6)]
_AFRR_MINUS_PARAMS = [(1.2, 0.6), (0.8, 0.6), (1.6, 0.6), (1.4, 0.6), (0.5, 0.6), (1.0, 0.6)]


def _load_env_file(path: Path) -> None:
    """Read KEY=VALUE lines from *path* into ``os.environ`` if not already set.

    Comments (``#``) and blank lines are ignored. Existing environment
    variables win, matching standard .env loader behavior.
    """
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _resolve_env_file(override: str | None) -> Path | None:
    if override:
        return Path(override)
    env_var = os.environ.get("ONPREM_ENV_FILE")
    if env_var:
        return Path(env_var)
    return None


def _build_timespan() -> TimeSpanRequest:
    return TimeSpanRequest(
        period_start=TARGET_DATE,
        period_end=TARGET_DATE + timedelta(days=1),
        resolution="15min",
    )


def _build_site() -> SiteRequest:
    """Binary 1 MW CHP with aFRR+/- abilities + gas/electricity/heat IO."""
    chp = CHPDevice(
        name="CHP-bin",
        properties=CHPProperties(
            gas_input=2.5,
            el_output=1.0,
            heat_output=1.0,
            is_binary=True,
            max_starts_per_day=3,
            ans_abilities=[
                ANSAbility(service="afrr_plus", min_device_power_rate=0.0, max_device_power_rate=1.0),
                ANSAbility(service="afrr_minus", min_device_power_rate=0.0, max_device_power_rate=1.0),
            ],
        ),
    )
    gas = GasImportDevice(
        name="Gas",
        properties=GasImportProperties(
            price=[45.0] * 96,
            max_import=2.5,
            max_import_total=15.0,
        ),
    )
    el_export = ElectricityExportDevice(
        name="ElExport",
        properties=ElectricityExportProperties(
            price=list(_DA_PROFILE_24H_15MIN),
            max_export=1.0,
        ),
    )
    heat_export = HeatExportDevice(
        name="HeatExport",
        properties=HeatExportProperties(
            price=[5.0] * 96,
            max_export=1.0,
        ),
    )
    return SiteRequest(
        site_id="test-site",
        devices=[chp, gas, el_export, heat_export],
    )


def _build_acceptance(timespan: TimeSpanRequest) -> list[BidAcceptanceEntry]:
    blocks = four_hour_block_starts(timespan)
    entries: list[BidAcceptanceEntry] = []
    for block, (plus, minus) in zip(blocks, zip(_AFRR_PLUS_PARAMS, _AFRR_MINUS_PARAMS, strict=True), strict=True):
        entries.append(
            BidAcceptanceEntry(
                service="afrr_plus",
                interval_start=block,
                distribution=LogNormalParams(mu=plus[0], sigma=plus[1]),
            )
        )
        entries.append(
            BidAcceptanceEntry(
                service="afrr_minus",
                interval_start=block,
                distribution=LogNormalParams(mu=minus[0], sigma=minus[1]),
            )
        )
    return entries


def _build_plan_request() -> ReservationBidPlanRequest:
    timespan = _build_timespan()
    return ReservationBidPlanRequest(
        sites=[_build_site()],
        timespan=timespan,
        services=["afrr_plus", "afrr_minus"],
        acceptance=_build_acceptance(timespan),
    )


def _print(label: str, msg: str) -> None:
    print(f"[{label}] {msg}")


def _check(condition: bool, msg: str) -> None:
    if not condition:
        raise AssertionError(msg)


def run(base_url: str, api_key: str) -> None:
    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    plan_key = f"py-rb-prod-{ts_tag}-plan"

    plan_req = _build_plan_request()
    plan_req_dict = plan_req.model_dump(mode="json")

    with OnPremClient(base_url=base_url, api_key=api_key, timeout_seconds=600.0) as client:
        # --- [1/7] health -------------------------------------------------
        _print("1/8", f"GET {base_url}/v1/health")
        h = client.health()
        _check(h.status == "ok", f"health status not ok: {h.status}")
        _check(h.db_ok is True, "db_ok is False")
        _print(
            "1/8",
            f"  status={h.status}  service_version={h.service_version}  "
            f"site_calc={h.site_calc_version}  db_ok={h.db_ok}",
        )

        # --- [2/7] planner -----------------------------------------------
        _print("2/8", f"POST /v1/reservation-bids -- Idempotency-Key: {plan_key}")
        t0 = time.monotonic()
        raw_plan = client.build_reservation_bids(plan_req_dict, idempotency_key=plan_key)
        elapsed = time.monotonic() - t0
        plan = ReservationBidPlanResult.model_validate(raw_plan)
        _check(1 <= len(plan.bids) <= 6, f"bid count out of range: {len(plan.bids)}")
        _check(plan.expected_revenue > 0, f"non-positive expected_revenue: {plan.expected_revenue}")
        _check(plan.diagnostics.get("winner_is_maximal") is True, f"winner not maximal: {plan.diagnostics}")
        mpr_bundled = plan.most_probable_realization
        _check(
            0.0 < mpr_bundled.joint_probability <= 1.0,
            f"P(joint) out of range: {mpr_bundled.joint_probability}",
        )
        _check(
            math.isclose(plan.evaluation.expected_revenue, plan.expected_revenue, rel_tol=1e-9),
            f"evaluation revenue mismatch: {plan.evaluation.expected_revenue} vs {plan.expected_revenue}",
        )
        _print(
            "2/8",
            f"  bids={len(plan.bids)}  expected_revenue={plan.expected_revenue:.2f} EUR  "
            f"variants={plan.diagnostics.get('variant_count')}  wall={elapsed:.1f}s",
        )

        # --- [3/7] idempotency replay ------------------------------------
        _print("3/8", "POST /v1/reservation-bids (same Idempotency-Key) -- replay expected")
        raw_replay = client.build_reservation_bids(plan_req_dict, idempotency_key=plan_key)
        replay_header = client.last_response_headers.get("x-idempotent-replay")
        _check(
            replay_header == "true",
            f"X-Idempotent-Replay header missing or wrong: {replay_header!r}",
        )
        _check(raw_replay == raw_plan, "replay body differs from original")
        _print("3/8", "  X-Idempotent-Replay: true; body matches (no second planner run charged)")

        # --- [4/7] evaluate ----------------------------------------------
        _print("4/8", "POST /v1/reservation-bids/evaluate (planner's own bids)")
        # plan.bids are ReservationBidOut; the evaluate request takes
        # ReservationBidIn. Round-trip through model_dump to coerce -- they
        # share field names.
        plan_bids_as_input = [ReservationBidIn.model_validate(b.model_dump()) for b in plan.bids]
        eval_req = ReservationBidEvaluateRequest(
            sites=plan_req.sites,
            timespan=plan_req.timespan,
            bids=plan_bids_as_input,
            acceptance=plan_req.acceptance,
        )
        raw_eval = client.evaluate_reservation_bids(eval_req.model_dump(mode="json"))
        eval_rev = float(raw_eval["expected_revenue"])
        _check(
            math.isclose(eval_rev, plan.expected_revenue, rel_tol=1e-9),
            f"evaluate disagrees with planner: {eval_rev} vs {plan.expected_revenue}",
        )
        _print("4/8", f"  expected_revenue={eval_rev:.6f} EUR (matches planner bit-exactly)")

        # --- [5/7] MPR standalone ----------------------------------------
        _print("5/8", "POST /v1/reservation-bids/most-probable-realization")
        # Server's MPR endpoint takes the same shape as evaluate, minus
        # ``expected_activation_revenue``. Reuse the typed evaluate payload.
        mpr_req = ReservationBidMPRRequest(
            sites=plan_req.sites,
            timespan=plan_req.timespan,
            bids=plan_bids_as_input,
            acceptance=plan_req.acceptance,
        )
        raw_mpr = client.most_probable_realization(mpr_req.model_dump(mode="json"))
        for key in ("joint_probability", "baseline_da", "realized_revenue"):
            standalone = float(raw_mpr[key])
            bundled = float(getattr(mpr_bundled, key))
            _check(
                math.isclose(standalone, bundled, rel_tol=1e-9),
                f"MPR {key} mismatch: standalone={standalone} bundled={bundled}",
            )
        _print(
            "5/8",
            f"  contracts={len(raw_mpr['contracts'])}  "
            f"realized_revenue={raw_mpr['realized_revenue']:.2f}  "
            f"P(joint)={raw_mpr['joint_probability']:.3f}",
        )

        # --- [6/8] temporal constraints round-trip ----------------------
        _print(
            "6/8",
            "POST /v1/reservation-bids with must_run / must_be_idle / min_continuous_run_hours set",
        )
        # Evening peak (16:00-20:00) is the day's most profitable block in
        # the baseline DA profile, so forcing the CHP idle there must lower
        # the planner's expected revenue meaningfully -- the cleanest end-to-
        # end signal that the new fields traversed wire -> translator -> LP.
        # 15-minute resolution: each 4-hour block is 16 intervals.
        evening_peak_idle = [0] * 64 + [1] * 16 + [0] * 16
        morning_must_run = [0] * 16 + [1] * 16 + [0] * 64  # 04:00-08:00, profitable anyway
        constrained_req = deepcopy(plan_req_dict)
        constrained_props = constrained_req["sites"][0]["devices"][0]["properties"]
        constrained_props["must_be_idle"] = evening_peak_idle
        constrained_props["must_run"] = morning_must_run
        constrained_props["min_continuous_run_hours"] = 2.0
        constrained_key = f"py-rb-prod-{ts_tag}-constrained"
        raw_constrained = client.build_reservation_bids(constrained_req, idempotency_key=constrained_key)
        constrained_plan = ReservationBidPlanResult.model_validate(raw_constrained)
        _check(
            constrained_plan.expected_revenue > 0,
            f"constrained expected_revenue non-positive: {constrained_plan.expected_revenue}",
        )
        _check(
            constrained_plan.expected_revenue < plan.expected_revenue - 1.0,
            f"must_be_idle on evening peak should lower revenue; "
            f"baseline={plan.expected_revenue:.2f}, constrained={constrained_plan.expected_revenue:.2f}",
        )
        _print(
            "6/8",
            f"  baseline={plan.expected_revenue:.2f} EUR  "
            f"constrained={constrained_plan.expected_revenue:.2f} EUR  "
            f"delta={plan.expected_revenue - constrained_plan.expected_revenue:.2f} EUR",
        )

        # --- [7/8] 422 TRANSLATION_ERROR on missing ANS ------------------
        _print("7/8", "expect 422 TRANSLATION_ERROR when ans_abilities is stripped")
        bad_req = deepcopy(plan_req_dict)
        bad_req["sites"][0]["devices"][0]["properties"]["ans_abilities"] = []
        try:
            client.build_reservation_bids(bad_req)
        except ValidationError as exc:
            _check(
                exc.code == "TRANSLATION_ERROR",
                f"expected TRANSLATION_ERROR, got code={exc.code!r} message={exc.message!r}",
            )
            _print("7/8", f"  HTTP 422 TRANSLATION_ERROR (as expected): {exc}")
        except OnPremError as exc:  # pragma: no cover - other 4xx surfaces here
            raise AssertionError(f"expected ValidationError 422, got {type(exc).__name__}: {exc}") from exc
        else:
            raise AssertionError("expected ValidationError 422; server returned 200")

        # --- [8/8] 401 with bogus bearer ---------------------------------
        _print("8/8", "expect 401 with a bogus bearer")
    # Re-enter with a fresh client to avoid polluting the good client's
    # state with the bad bearer (the OnPremClient header is set per-instance
    # at construction time, so this is a clean second client).
    with OnPremClient(
        base_url=base_url,
        api_key="op_definitely_not_a_real_key",
        timeout_seconds=30.0,
        busy_retry=None,
    ) as bogus:
        try:
            bogus.build_reservation_bids(plan_req_dict)
        except AuthenticationError as exc:
            _print("8/8", f"  HTTP 401 (as expected): {exc}")
        except OnPremError as exc:  # pragma: no cover
            raise AssertionError(f"expected AuthenticationError 401, got {type(exc).__name__}: {exc}") from exc
        else:
            raise AssertionError("expected AuthenticationError 401; server returned 200")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--env-file",
        default=None,
        help="Path to a KEY=VALUE file holding ONPREM_API_KEY and (optionally) ONPREM_BASE_URL. "
        "Equivalent to setting the ONPREM_ENV_FILE environment variable.",
    )
    args = parser.parse_args(argv)

    env_file = _resolve_env_file(args.env_file)
    if env_file is not None and env_file.exists():
        _load_env_file(env_file)
        print(f"[env] loaded {env_file}")
    elif args.env_file:
        print(f"[FAIL] --env-file {args.env_file} not found", file=sys.stderr)
        return 2

    api_key = os.environ.get("ONPREM_API_KEY")
    if not api_key:
        print(
            "[FAIL] ONPREM_API_KEY is required (set it in the environment, "
            "or point --env-file / ONPREM_ENV_FILE at a KEY=VALUE file)",
            file=sys.stderr,
        )
        return 2
    base_url = os.environ.get("ONPREM_BASE_URL", "https://operational.algoenergy.cz").rstrip("/")
    print(f"[env] target {base_url}")

    try:
        run(base_url, api_key)
    except AssertionError as exc:
        print(f"\nRESERVATION-BIDS PROD TEST (PY) FAIL: {exc}", file=sys.stderr)
        print(f"URL: {base_url}", file=sys.stderr)
        return 1
    except OnPremError as exc:
        print(f"\nRESERVATION-BIDS PROD TEST (PY) FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"URL: {base_url}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - surface unexpected failures
        print(f"\nRESERVATION-BIDS PROD TEST (PY) FAIL: unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"URL: {base_url}", file=sys.stderr)
        return 1

    print()
    print("RESERVATION-BIDS PROD TEST (PY) PASS")
    print(f"URL: {base_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
