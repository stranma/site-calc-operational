"""Convenience helpers for constructing reservation-bid requests.

The on-prem server's wire schema enforces specific shapes (single-day
timespan, 4-hour acceptance blocks at 00/04/08/12/16/20 local) that are
verbose to spell out by hand and easy to get wrong (off-by-block,
wrong-timezone). These helpers encode the conventions once.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from site_calc_operational.models._base import ServiceCode
from site_calc_operational.models.reservation_bids import (
    AcceptanceDistributionInput,
    ActivationRevenueEntry,
    BidAcceptanceEntry,
    TimeSpanRequest,
)


def four_hour_block_starts(timespan: TimeSpanRequest) -> list[datetime]:
    """The six 4-hour block starts of the timespan's day.

    Returns ``[00:00, 04:00, 08:00, 12:00, 16:00, 20:00]`` of the day named
    by ``timespan.period_start``, preserving its timezone. The on-prem
    server's reservation-bid endpoints require each ``BidAcceptanceEntry
    .interval_start`` to be one of these six values; this helper makes that
    explicit instead of leaving the caller to compute the offsets.

    :param timespan: The request timespan. ``period_start`` must be at
        local-tz midnight; the helper does not enforce that (the server
        will, with a 422 ``TRANSLATION_ERROR``).
    :returns: List of six ``datetime`` values in the same timezone as
        ``timespan.period_start``, one per 4-hour block.

    Example::

        ts = TimeSpanRequest(
            period_start=datetime(2026, 5, 13, 0, 0, tzinfo=ZoneInfo("Europe/Prague")),
            period_end=datetime(2026, 5, 14, 0, 0, tzinfo=ZoneInfo("Europe/Prague")),
            resolution="15min",
        )
        blocks = four_hour_block_starts(ts)
        # [2026-05-13T00:00+02:00, 2026-05-13T04:00+02:00, ..., 2026-05-13T20:00+02:00]
    """
    start = timespan.period_start
    return [start + timedelta(hours=4 * i) for i in range(6)]


def build_uniform_acceptance(
    timespan: TimeSpanRequest,
    services: list[ServiceCode],
    distribution: AcceptanceDistributionInput,
) -> list[BidAcceptanceEntry]:
    """Construct the full ``acceptance`` list with the same distribution
    everywhere.

    Useful as a starting point when the operator has no per-block forecast
    and just wants "this distribution for every (service, block)". For
    real production use, you almost always want per-block parameters --
    daily price shape is strongly bimodal in Czech aFRR markets.

    :param timespan: Single-day timespan.
    :param services: Services to bid into (e.g. ``["afrr_plus", "afrr_minus"]``).
    :param distribution: The distribution to use for every entry.
    :returns: ``len(services) * 6`` entries -- the full Cartesian product
        the on-prem planner requires.

    Example::

        from site_calc_operational.models import LogNormalParams, build_uniform_acceptance

        acceptance = build_uniform_acceptance(
            timespan=ts,
            services=["afrr_plus", "afrr_minus"],
            distribution=LogNormalParams.from_mean_cv(mean=8.0, cv=0.6),
        )
    """
    blocks = four_hour_block_starts(timespan)
    return [
        BidAcceptanceEntry(service=svc, interval_start=block, distribution=distribution)
        for svc in services
        for block in blocks
    ]


def build_zero_activation_revenue(
    timespan: TimeSpanRequest,
    services: list[ServiceCode],
) -> list[ActivationRevenueEntry]:
    """Construct the full ``expected_activation_revenue`` list as all zeros.

    The conservative "no activation upside expected" default. Useful when
    the operator has no activation forecast and wants the planner to value
    each bid by its capacity payment alone.

    :param timespan: Single-day timespan.
    :param services: Services to bid into.
    :returns: ``len(services) * 6`` entries, all with ``eur_per_mw_h=0.0``.
    """
    blocks = four_hour_block_starts(timespan)
    return [
        ActivationRevenueEntry(service=svc, interval_start=block, eur_per_mw_h=0.0)
        for svc in services
        for block in blocks
    ]


__all__ = [
    "four_hour_block_starts",
    "build_uniform_acceptance",
    "build_zero_activation_revenue",
]
