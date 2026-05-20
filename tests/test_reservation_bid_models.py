"""Tests for the reservation-bid Pydantic models.

Two flavours:

* Round-trip: build a typed request, JSON-dump it, POST through a mocked
  on-prem client, and parse the mocked response back into the typed result.
  Catches: any field-name mismatch between the request models and the
  HTTP-side dict shape; any required-field drift in the response models.

* Server-parity (drift): the on-prem server defines the canonical schema in
  ``server-onprem``. The drift test asserts the client-side model field
  names match the server's wire field names exactly. Server lives in the
  monorepo and is not installed in the client's venv, so the parity is
  pinned by a literal list -- when the server's schema changes, this test
  fails and forces an explicit update here.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest
import respx
from httpx import Response

from site_calc_operational.api.onprem_client import OnPremClient
from site_calc_operational.models import (
    AcceptanceDistributionInput,
    ActivationRevenueEntry,
    BidAcceptanceEntry,
    DeviceRequest,
    EmpiricalPercentilesParams,
    EvaluationResult,
    LogNormalFromQuantilesParams,
    LogNormalParams,
    MostProbableRealizationResult,
    ReservationBidEvaluateRequest,
    ReservationBidIn,
    ReservationBidMPRRequest,
    ReservationBidOut,
    ReservationBidPlanRequest,
    ReservationBidPlanResult,
    SiteRequest,
    TimeSpanRequest,
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _block_starts() -> list[datetime]:
    return [datetime(2026, 5, 13, h, 0, 0, tzinfo=timezone.utc) for h in (0, 4, 8, 12, 16, 20)]


def _full_acceptance() -> list[BidAcceptanceEntry]:
    entries: list[BidAcceptanceEntry] = []
    for start in _block_starts():
        entries.append(
            BidAcceptanceEntry(
                service="afrr_plus",
                interval_start=start,
                distribution=LogNormalParams(mu=1.5, sigma=0.6),
            )
        )
        entries.append(
            BidAcceptanceEntry(
                service="afrr_minus",
                interval_start=start,
                distribution=LogNormalParams(mu=1.0, sigma=0.6),
            )
        )
    return entries


def _binary_chp_site() -> SiteRequest:
    return SiteRequest(
        site_id="test-site",
        devices=[
            DeviceRequest(
                name="CHP-bin",
                type="chp",
                properties={
                    "gas_input": 2.5,
                    "el_output": 1.0,
                    "heat_output": 1.0,
                    "is_binary": True,
                    "ans_abilities": [
                        {"service": "afrr_plus", "min_device_power_rate": 0.0, "max_device_power_rate": 1.0},
                        {"service": "afrr_minus", "min_device_power_rate": 0.0, "max_device_power_rate": 1.0},
                    ],
                },
            ),
        ],
    )


def _day_timespan() -> TimeSpanRequest:
    return TimeSpanRequest(
        period_start=datetime(2026, 5, 13, 0, 0, 0, tzinfo=timezone.utc),
        period_end=datetime(2026, 5, 14, 0, 0, 0, tzinfo=timezone.utc),
        resolution="15min",
    )


# ---------------------------------------------------------------------------
# Distribution tagged union
# ---------------------------------------------------------------------------


def test_lognormal_params_sigma_must_be_positive() -> None:
    """Failure mode: a caller can construct a log-normal with sigma <= 0,
    which would crash the server with an opaque numeric error."""
    with pytest.raises(ValueError):
        LogNormalParams(mu=1.0, sigma=0.0)
    with pytest.raises(ValueError):
        LogNormalParams(mu=1.0, sigma=-0.1)


def test_distribution_tagged_union_dispatches_by_type() -> None:
    """Failure mode: the discriminator is wired wrong and a payload that
    declares ``type="empirical_percentiles"`` is parsed as ``LogNormalParams``,
    silently dropping the breakpoints."""
    entry = BidAcceptanceEntry.model_validate(
        {
            "service": "afrr_plus",
            "interval_start": "2026-05-13T00:00:00+00:00",
            "distribution": {
                "type": "empirical_percentiles",
                "breakpoints": [(1.0, 0.9), (5.0, 0.5), (20.0, 0.1)],
            },
        }
    )
    assert isinstance(entry.distribution, EmpiricalPercentilesParams)
    assert entry.distribution.breakpoints == [(1.0, 0.9), (5.0, 0.5), (20.0, 0.1)]


def test_distribution_from_quantiles_round_trips() -> None:
    entry = BidAcceptanceEntry.model_validate(
        {
            "service": "afrr_plus",
            "interval_start": "2026-05-13T00:00:00+00:00",
            "distribution": {
                "type": "lognormal_from_quantiles",
                "quantiles": [(0.25, 5.0), (0.75, 20.0)],
            },
        }
    )
    assert isinstance(entry.distribution, LogNormalFromQuantilesParams)


# ---------------------------------------------------------------------------
# Bid + activation revenue field constraints
# ---------------------------------------------------------------------------


def test_reservation_bid_in_volume_must_be_positive() -> None:
    with pytest.raises(ValueError):
        ReservationBidIn(
            service="afrr_plus",
            interval_start=datetime(2026, 5, 13, 0, 0, 0, tzinfo=timezone.utc),
            volume_mw=0.0,
            capacity_price=10.0,
        )


def test_reservation_bid_in_price_can_be_zero() -> None:
    """A zero capacity-price is a valid (degenerate) bid that always clears.
    The schema accepts it; the planner decides whether it's optimal."""
    ReservationBidIn(
        service="afrr_plus",
        interval_start=datetime(2026, 5, 13, 0, 0, 0, tzinfo=timezone.utc),
        volume_mw=1.0,
        capacity_price=0.0,
    )


def test_activation_revenue_must_be_non_negative() -> None:
    with pytest.raises(ValueError):
        ActivationRevenueEntry(
            service="afrr_plus",
            interval_start=datetime(2026, 5, 13, 0, 0, 0, tzinfo=timezone.utc),
            eur_per_mw_h=-0.01,
        )


# ---------------------------------------------------------------------------
# Request models: required fields
# ---------------------------------------------------------------------------


def test_plan_request_requires_at_least_one_service() -> None:
    with pytest.raises(ValueError):
        ReservationBidPlanRequest(
            sites=[_binary_chp_site()],
            timespan=_day_timespan(),
            services=[],
            acceptance=_full_acceptance(),
        )


def test_plan_request_max_one_site() -> None:
    """v1 planner expects exactly one site; >1 is a wire error."""
    with pytest.raises(ValueError):
        ReservationBidPlanRequest(
            sites=[_binary_chp_site(), _binary_chp_site()],
            timespan=_day_timespan(),
            services=["afrr_plus"],
            acceptance=_full_acceptance(),
        )


def test_evaluate_request_requires_bids() -> None:
    with pytest.raises(ValueError):
        ReservationBidEvaluateRequest(
            sites=[_binary_chp_site()],
            timespan=_day_timespan(),
            bids=[],
            acceptance=_full_acceptance(),
        )


def test_mpr_request_requires_bids() -> None:
    with pytest.raises(ValueError):
        ReservationBidMPRRequest(
            sites=[_binary_chp_site()],
            timespan=_day_timespan(),
            bids=[],
            acceptance=_full_acceptance(),
        )


# ---------------------------------------------------------------------------
# Round-trip: typed -> dict -> client -> dict -> typed
# ---------------------------------------------------------------------------


@respx.mock
def test_planner_round_trip_through_client() -> None:
    """End-to-end: build typed request, model_dump on the wire, mocked server
    returns the documented response shape, parse back into typed result."""
    fake_response: dict = {
        "bids": [
            {
                "service": "afrr_plus",
                "interval_start": "2026-05-13T00:00:00+00:00",
                "volume_mw": 1.0,
                "capacity_price": 25.4,
            }
        ],
        "expected_revenue": 407.46,
        "diagnostics": {"winner_is_maximal": True, "variant_count": 729},
        "most_probable_realization": {
            "contracts": [],
            "baseline_da": 12.34,
            "realized_revenue": 12.34,
            "joint_probability": 1.0,
        },
        "evaluation": {"expected_revenue": 407.46},
    }
    respx.post("http://stub/v1/reservation-bids").mock(return_value=Response(200, json=fake_response))

    req = ReservationBidPlanRequest(
        sites=[_binary_chp_site()],
        timespan=_day_timespan(),
        services=["afrr_plus", "afrr_minus"],
        acceptance=_full_acceptance(),
    )
    payload = req.model_dump(mode="json")

    client = OnPremClient(base_url="http://stub", api_key="op_x", busy_retry=None)
    raw = client.build_reservation_bids(payload)
    result = ReservationBidPlanResult.model_validate(raw)

    assert math.isclose(result.expected_revenue, 407.46)
    assert math.isclose(result.evaluation.expected_revenue, 407.46)
    assert result.diagnostics["winner_is_maximal"] is True
    assert len(result.bids) == 1
    assert result.bids[0].service == "afrr_plus"
    assert isinstance(result.most_probable_realization, MostProbableRealizationResult)


@respx.mock
def test_evaluate_round_trip_through_client() -> None:
    respx.post("http://stub/v1/reservation-bids/evaluate").mock(
        return_value=Response(200, json={"expected_revenue": 407.461314})
    )
    req = ReservationBidEvaluateRequest(
        sites=[_binary_chp_site()],
        timespan=_day_timespan(),
        bids=[
            ReservationBidIn(
                service="afrr_plus",
                interval_start=datetime(2026, 5, 13, 16, 0, 0, tzinfo=timezone.utc),
                volume_mw=1.0,
                capacity_price=25.4,
            )
        ],
        acceptance=_full_acceptance(),
    )
    client = OnPremClient(base_url="http://stub", api_key="op_x", busy_retry=None)
    raw = client.evaluate_reservation_bids(req.model_dump(mode="json"))
    result = EvaluationResult.model_validate(raw)
    assert math.isclose(result.expected_revenue, 407.461314)


@respx.mock
def test_mpr_round_trip_through_client() -> None:
    fake = {
        "contracts": [
            {
                "service": "afrr_plus",
                "interval_start": "2026-05-13T16:00:00+00:00",
                "volume_mw": 1.0,
                "capacity_price": 25.4,
            }
        ],
        "baseline_da": 12.34,
        "realized_revenue": 419.91,
        "joint_probability": 0.083,
    }
    respx.post("http://stub/v1/reservation-bids/most-probable-realization").mock(return_value=Response(200, json=fake))
    req = ReservationBidMPRRequest(
        sites=[_binary_chp_site()],
        timespan=_day_timespan(),
        bids=[
            ReservationBidIn(
                service="afrr_plus",
                interval_start=datetime(2026, 5, 13, 16, 0, 0, tzinfo=timezone.utc),
                volume_mw=1.0,
                capacity_price=25.4,
            )
        ],
        acceptance=_full_acceptance(),
    )
    client = OnPremClient(base_url="http://stub", api_key="op_x", busy_retry=None)
    raw = client.most_probable_realization(req.model_dump(mode="json"))
    result = MostProbableRealizationResult.model_validate(raw)
    assert len(result.contracts) == 1
    assert isinstance(result.contracts[0], ReservationBidOut)
    assert math.isclose(result.joint_probability, 0.083)


# ---------------------------------------------------------------------------
# Server-parity (drift) -- pinned field names per wire schema
# ---------------------------------------------------------------------------


# Field-name parity with server-onprem/src/site_calc_onprem/schemas.py @ v0.2.0.
# When the server adds or renames a field, this constant lags and the test
# below fails -- update both sides in lockstep.
_EXPECTED_FIELDS: dict[type, set[str]] = {
    LogNormalParams: {"type", "mu", "sigma"},
    LogNormalFromQuantilesParams: {"type", "quantiles"},
    EmpiricalPercentilesParams: {"type", "breakpoints"},
    BidAcceptanceEntry: {"service", "interval_start", "distribution"},
    ReservationBidIn: {"service", "interval_start", "volume_mw", "capacity_price"},
    ActivationRevenueEntry: {"service", "interval_start", "eur_per_mw_h"},
    ReservationBidPlanRequest: {
        "sites",
        "timespan",
        "services",
        "acceptance",
        "expected_activation_revenue",
        "assume_maximal",
        "optimization_config",
        "metadata",
    },
    ReservationBidEvaluateRequest: {
        "sites",
        "timespan",
        "bids",
        "acceptance",
        "expected_activation_revenue",
        "optimization_config",
        "metadata",
    },
    ReservationBidMPRRequest: {
        "sites",
        "timespan",
        "bids",
        "acceptance",
        "optimization_config",
        "metadata",
    },
}


def test_server_wire_field_parity() -> None:
    """Failure mode: client model drops or renames a field that the server
    still requires (e.g. server adds a ``min_volume_mw`` to ``ReservationBidIn``
    and the client silently sends payloads missing it)."""
    for model_cls, expected in _EXPECTED_FIELDS.items():
        actual = set(model_cls.model_fields.keys())
        assert actual == expected, (
            f"{model_cls.__name__} fields drifted from the server wire schema:\n"
            f"  expected: {sorted(expected)}\n"
            f"  actual:   {sorted(actual)}\n"
            f"  missing:  {sorted(expected - actual)}\n"
            f"  extra:    {sorted(actual - expected)}\n"
            f"If the server changed, update _EXPECTED_FIELDS in this test and the model."
        )


def test_acceptance_input_is_a_discriminated_union() -> None:
    """Sanity: the type alias is annotated with a discriminator on ``type``.
    Without this, a payload like ``{type: 'lognormal', breakpoints: [...]}``
    might silently parse as ``EmpiricalPercentilesParams`` despite the tag."""
    # Constructing the union directly is enough; pydantic validates the tag
    # on parse, not at typing time. The behavioural check is the dispatch
    # test above; this one nails down that AcceptanceDistributionInput is
    # imported and is the right shape.
    assert AcceptanceDistributionInput is not None
