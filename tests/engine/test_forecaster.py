"""Monte Carlo forecaster (DESIGN §10.7) and confidence policy (§3.1)."""

from datetime import UTC, date, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from factories import task
from pulseboard.domain.plan import Forecast
from pulseboard.domain.snapshot import Task
from pulseboard.domain.types import Confidence, ForecastMethod, StatusCategory
from pulseboard.engine.calendar import WorkCalendar
from pulseboard.engine.forecaster import (
    Throughput,
    choose_primary,
    monte_carlo_forecast,
    simulate_weeks,
    weekly_throughput,
    weeks_to_date,
)

PLAN = date(2026, 10, 19)
CAL = WorkCalendar(plan_date=PLAN, team_holiday_country="US", hours_per_day=8, horizon_years=1)
DONE = StatusCategory.DONE


def resolved(issue_id: int, day: date) -> Task:
    return task(
        issue_id,
        category=DONE,
        created=datetime(2026, 9, 1, tzinfo=UTC),
        resolved=datetime(day.year, day.month, day.day, 12, tzinfo=UTC),
    )


def test_weekly_throughput_hand_computed() -> None:
    # Board exists since Sep 1: 48 days → 7 weeks of history. Week 0 = Oct 13..19.
    tasks = (
        resolved(1, date(2026, 10, 19)),  # week 0
        resolved(2, date(2026, 10, 13)),  # week 0
        resolved(3, date(2026, 10, 12)),  # week 1
        resolved(4, date(2026, 9, 28)),  # week 3
        resolved(5, date(2026, 10, 20)),  # after the plan date: ignored
        task(6, created=datetime(2026, 9, 1, tzinfo=UTC)),  # open: ignored
    )
    throughput = weekly_throughput(tasks, PLAN, "UTC", history_weeks=12, window_days=None)

    assert throughput.weekly == (0, 0, 0, 1, 0, 1, 2)
    assert throughput.weeks == 7


def test_history_is_capped_at_the_setting() -> None:
    tasks = (resolved(1, date(2026, 10, 19)),)

    assert weekly_throughput(tasks, PLAN, "UTC", history_weeks=3, window_days=None).weeks == 3
    assert weekly_throughput((), PLAN, "UTC", history_weeks=3, window_days=None).weeks == 0


def test_fixed_seed_reproduces_percentiles() -> None:
    first = simulate_weeks(20, (1, 3, 0, 2, 4), trials=10_000, seed=42)

    assert first == (10, 13, 15)
    assert simulate_weeks(20, (1, 3, 0, 2, 4), trials=10_000, seed=42) == first


def test_constant_throughput_is_exact() -> None:
    assert simulate_weeks(5, (2, 2, 2), trials=1000, seed=0) == (3, 3, 3)


def test_nothing_remaining_or_no_throughput() -> None:
    assert simulate_weeks(0, (), trials=10, seed=0) == (0, 0, 0)
    assert simulate_weeks(3, (0, 0), trials=10, seed=0) is None
    assert simulate_weeks(3, (), trials=10, seed=0) is None


@settings(max_examples=40, deadline=None)
@given(
    remaining=st.integers(min_value=0, max_value=60),
    weekly=st.lists(st.integers(min_value=0, max_value=8), min_size=1, max_size=12),
    seed=st.integers(min_value=0, max_value=2**32),
)
def test_percentiles_are_ordered_and_reproducible(
    remaining: int, weekly: list[int], seed: int
) -> None:
    result = simulate_weeks(remaining, weekly, trials=300, seed=seed)
    if result is None:
        assert sum(weekly) == 0
        return
    p50, p85, p95 = result
    assert p50 <= p85 <= p95
    assert simulate_weeks(remaining, weekly, trials=300, seed=seed) == result


def test_weeks_to_date() -> None:
    assert weeks_to_date(0, CAL) == date(2026, 10, 19)
    assert weeks_to_date(1, CAL) == date(2026, 10, 23)
    assert weeks_to_date(2, CAL) == date(2026, 10, 30)


def test_forecast_confidence_from_history() -> None:
    short = monte_carlo_forecast(
        4, Throughput((2, 2)), CAL, trials=100, seed=1, min_history_weeks=4
    )
    long = monte_carlo_forecast(
        4, Throughput((2,) * 6), CAL, trials=100, seed=1, min_history_weeks=4
    )

    assert short is not None
    assert long is not None
    assert short.confidence is Confidence.LOW
    assert long.confidence is Confidence.MEDIUM
    assert long.method is ForecastMethod.MONTE_CARLO
    assert long.p50 == date(2026, 10, 30)
    assert long.trial_count == 100
    assert (
        monte_carlo_forecast(4, Throughput((0,)), CAL, trials=10, seed=1, min_history_weeks=4)
        is None
    )


def schedule_fc() -> Forecast:
    end = date(2026, 11, 1)
    return Forecast(
        method=ForecastMethod.SCHEDULE,
        p50=end,
        p85=end,
        p95=end,
        confidence=Confidence.HIGH,
        confidence_reason="x",
    )


def test_primary_is_schedule_when_well_estimated() -> None:
    mc = monte_carlo_forecast(4, Throughput((2,) * 6), CAL, trials=10, seed=1, min_history_weeks=4)

    primary, secondary = choose_primary(schedule_fc(), mc, share=0.6, threshold=0.6)
    assert primary.method is ForecastMethod.SCHEDULE
    assert secondary is mc


def test_primary_is_monte_carlo_when_poorly_estimated() -> None:
    mc = monte_carlo_forecast(4, Throughput((2,) * 6), CAL, trials=10, seed=1, min_history_weeks=4)

    primary, secondary = choose_primary(schedule_fc(), mc, share=0.59, threshold=0.6)
    assert primary is mc
    assert secondary is not None
    assert secondary.method is ForecastMethod.SCHEDULE


def test_schedule_stays_primary_without_throughput() -> None:
    primary, secondary = choose_primary(schedule_fc(), None, share=0.1, threshold=0.6)

    assert primary.method is ForecastMethod.SCHEDULE
    assert secondary is None


def steady(weeks: int) -> list[Task]:
    """Two resolutions every week for ``weeks`` weeks up to the plan date (board from 2025)."""
    return [
        resolved(100 * week + n, PLAN - timedelta(days=7 * week + n)).model_copy(
            update={"created": datetime(2025, 1, 1, tzinfo=UTC)}
        )
        for week in range(weeks)
        for n in range(2)
    ]


@pytest.mark.parametrize(
    ("window_days", "weeks", "capped"),
    [(83, 11, True), (84, 12, False), (85, 12, False), (30, 4, True)],
)
def test_history_never_reaches_past_the_fetch_window(
    window_days: int, weeks: int, capped: bool
) -> None:
    """PB-08: resolutions older than the window were never fetched, so those weeks are left out
    instead of counted as zero; the forecast says the history was capped."""
    tasks = [t for t in steady(20) if (PLAN - t.resolved.date()).days <= window_days]  # type: ignore[union-attr]

    found = weekly_throughput(tasks, PLAN, "UTC", 12, window_days)
    forecast = monte_carlo_forecast(40, found, CAL, trials=500, seed=1, min_history_weeks=4)

    assert found.weeks == weeks
    assert 0 not in found.weekly
    assert (found.capped_from == 12) is capped
    assert forecast is not None
    assert ("capped from 12" in forecast.confidence_reason) is capped
