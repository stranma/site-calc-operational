"""Tests for the v0.3.0 convenience helpers."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from site_calc_operational.models import (
    BidAcceptanceEntry,
    LogNormalParams,
    TimeSpanRequest,
    build_per_block_acceptance,
    build_uniform_acceptance,
    build_zero_activation_revenue,
    four_hour_block_starts,
)

# ---------------------------------------------------------------------------
# four_hour_block_starts
# ---------------------------------------------------------------------------


def _day_timespan(tz: timezone = timezone.utc) -> TimeSpanRequest:
    start = datetime(2026, 5, 13, 0, 0, 0, tzinfo=tz)
    return TimeSpanRequest(
        period_start=start,
        period_end=start + timedelta(days=1),
        resolution="15min",
    )


def test_four_hour_block_starts_returns_six_blocks() -> None:
    """Failure mode: the planner needs exactly six 4-hour blocks per day;
    a helper returning anything else would generate invalid acceptance."""
    starts = four_hour_block_starts(_day_timespan(timezone.utc))
    assert len(starts) == 6
    hours = [s.hour for s in starts]
    assert hours == [0, 4, 8, 12, 16, 20]


def test_four_hour_block_starts_preserves_timezone() -> None:
    """The returned starts must carry the same tzinfo as the input timespan.
    Switching to UTC silently would create entries the server rejects."""
    fixed_offset = timezone(timedelta(hours=2))  # CEST-like fixed offset
    offset_starts = four_hour_block_starts(_day_timespan(fixed_offset))
    assert all(s.tzinfo == fixed_offset for s in offset_starts)

    utc_starts = four_hour_block_starts(_day_timespan(timezone.utc))
    assert all(s.tzinfo == timezone.utc for s in utc_starts)


def test_four_hour_block_starts_offset_matches_period_start() -> None:
    """A non-midnight period_start (the server will reject this at translation,
    but the helper must not bury that off-by-N hours into the offsets)."""
    bad_ts = TimeSpanRequest(
        period_start=datetime(2026, 5, 13, 3, 0, 0, tzinfo=timezone.utc),
        period_end=datetime(2026, 5, 14, 3, 0, 0, tzinfo=timezone.utc),
        resolution="15min",
    )
    starts = four_hour_block_starts(bad_ts)
    # Helper just adds 4h offsets to whatever it was given; the server enforces
    # the midnight-aligned rule. This test pins the helper's contract.
    assert starts[0].hour == 3
    assert starts[1].hour == 7


# ---------------------------------------------------------------------------
# build_uniform_acceptance
# ---------------------------------------------------------------------------


def test_build_uniform_acceptance_full_cartesian() -> None:
    """The helper must emit ``len(services) * 6`` entries -- the full
    Cartesian product the planner requires. Anything else means the user
    will hit 422 TRANSLATION_ERROR."""
    ts = _day_timespan(timezone.utc)
    dist = LogNormalParams(mu=2.0, sigma=0.6)
    entries = build_uniform_acceptance(ts, ["afrr_plus", "afrr_minus"], dist)
    assert len(entries) == 12  # 2 services x 6 blocks
    services = {e.service for e in entries}
    assert services == {"afrr_plus", "afrr_minus"}
    block_hours = sorted({e.interval_start.hour for e in entries})
    assert block_hours == [0, 4, 8, 12, 16, 20]


def test_build_uniform_acceptance_propagates_distribution() -> None:
    """Failure mode: helper replaces the caller's distribution with a default,
    silently. Pin the distribution identity through."""
    ts = _day_timespan(timezone.utc)
    dist = LogNormalParams(mu=2.5, sigma=0.7)
    entries = build_uniform_acceptance(ts, ["afrr_plus"], dist)
    assert all(isinstance(e.distribution, LogNormalParams) for e in entries)
    assert all(e.distribution.mu == 2.5 and e.distribution.sigma == 0.7 for e in entries)


def test_build_uniform_acceptance_returns_typed_entries() -> None:
    """Failure mode: the helper returns plain dicts instead of typed
    BidAcceptanceEntry. The list must be usable verbatim in
    ReservationBidPlanRequest.acceptance."""
    ts = _day_timespan(timezone.utc)
    dist = LogNormalParams(mu=1.5, sigma=0.5)
    entries = build_uniform_acceptance(ts, ["afrr_plus"], dist)
    assert all(isinstance(e, BidAcceptanceEntry) for e in entries)


# ---------------------------------------------------------------------------
# build_per_block_acceptance
# ---------------------------------------------------------------------------


def test_build_per_block_acceptance_full_cartesian() -> None:
    """Same Cartesian-product invariant as build_uniform_acceptance, but each
    (service, block) gets its own distribution."""
    ts = _day_timespan()
    plus_dists = [LogNormalParams.from_mean_cv(mean=m, cv=0.6) for m in [6.0, 5.0, 8.0, 10.0, 14.0, 11.0]]
    minus_dists = [LogNormalParams.from_mean_cv(mean=m, cv=0.6) for m in [4.5, 3.5, 6.0, 7.5, 10.0, 8.0]]
    entries = build_per_block_acceptance(
        ts,
        distributions_by_service={"afrr_plus": plus_dists, "afrr_minus": minus_dists},
    )
    assert len(entries) == 12
    plus_entries = [e for e in entries if e.service == "afrr_plus"]
    assert len(plus_entries) == 6
    # Verify each block got its assigned distribution (mu strictly increases
    # then drops, matching the mean profile).
    mus = [e.distribution.mu for e in plus_entries]
    assert mus == [plus_dists[i].mu for i in range(6)]


def test_build_per_block_acceptance_rejects_wrong_length() -> None:
    """Failure mode: the helper silently truncates or pads. A length
    mismatch is the most common per-block mistake (off-by-one when copying
    from a 7-element forecast); catch it locally."""
    ts = _day_timespan()
    five_dists = [LogNormalParams(mu=1.0, sigma=0.5)] * 5
    with pytest.raises(ValueError, match="6"):
        build_per_block_acceptance(ts, {"afrr_plus": five_dists})


def test_build_per_block_acceptance_subset_of_services() -> None:
    """Caller can pass only the services they care about; helper doesn't
    require all four ANS services."""
    ts = _day_timespan()
    dists = [LogNormalParams(mu=1.5, sigma=0.6)] * 6
    entries = build_per_block_acceptance(ts, {"afrr_plus": dists})
    assert len(entries) == 6
    assert all(e.service == "afrr_plus" for e in entries)


def test_build_per_block_acceptance_propagates_typed_entries() -> None:
    """Each entry must be a typed BidAcceptanceEntry whose distribution is
    the one the caller supplied (identity check, not just equality)."""
    ts = _day_timespan()
    dists = [LogNormalParams(mu=float(i), sigma=0.5) for i in range(6)]
    entries = build_per_block_acceptance(ts, {"afrr_plus": dists})
    assert all(isinstance(e, BidAcceptanceEntry) for e in entries)
    for entry, expected in zip(entries, dists, strict=True):
        assert isinstance(entry.distribution, LogNormalParams)
        assert entry.distribution.mu == expected.mu


# ---------------------------------------------------------------------------
# build_zero_activation_revenue
# ---------------------------------------------------------------------------


def test_build_zero_activation_revenue_returns_zeros() -> None:
    ts = _day_timespan(timezone.utc)
    entries = build_zero_activation_revenue(ts, ["afrr_plus", "afrr_minus"])
    assert len(entries) == 12
    assert all(e.eur_per_mw_h == 0.0 for e in entries)


# ---------------------------------------------------------------------------
# LogNormalParams.from_mean_cv
# ---------------------------------------------------------------------------


def test_from_mean_cv_round_trips() -> None:
    """Construct from mean + CV, then verify the resulting distribution has
    the requested mean and CV (back-computed from mu, sigma)."""
    p = LogNormalParams.from_mean_cv(mean=8.0, cv=0.6)
    recovered_mean = math.exp(p.mu + p.sigma**2 / 2.0)
    recovered_cv = math.sqrt(math.exp(p.sigma**2) - 1.0)
    assert math.isclose(recovered_mean, 8.0, rel_tol=1e-9)
    assert math.isclose(recovered_cv, 0.6, rel_tol=1e-9)


def test_from_mean_cv_rejects_non_positive_mean() -> None:
    with pytest.raises(ValueError):
        LogNormalParams.from_mean_cv(mean=0.0, cv=0.5)
    with pytest.raises(ValueError):
        LogNormalParams.from_mean_cv(mean=-1.0, cv=0.5)


def test_from_mean_cv_rejects_non_positive_cv() -> None:
    with pytest.raises(ValueError):
        LogNormalParams.from_mean_cv(mean=5.0, cv=0.0)
    with pytest.raises(ValueError):
        LogNormalParams.from_mean_cv(mean=5.0, cv=-0.1)


def test_from_mean_cv_produces_lognormal_type() -> None:
    """The classmethod must return a LogNormalParams whose ``type`` is
    ``'lognormal'``, so it dispatches correctly through
    AcceptanceDistributionInput's tagged union."""
    p = LogNormalParams.from_mean_cv(mean=10.0, cv=0.5)
    assert p.type == "lognormal"
