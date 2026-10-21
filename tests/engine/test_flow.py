"""Flow metrics on fixture transitions, against hand-computed values (DESIGN §10.8)."""

from datetime import date

import pytest

from factories import FLOW_PLAN_DATE as PLAN
from factories import flow_board as board
from factories import snapshot, task
from pulseboard.domain.types import StatusCategory
from pulseboard.engine.flow import CfdPoint, flow_metrics, status_categories


def test_cumulative_flow() -> None:
    cfd = flow_metrics(board(), PLAN, "UTC", window_days=3).cfd
    by_day = {(p.day, p.column): p.count for p in cfd}

    def day(d: int) -> list[int]:
        return [by_day[(date(2026, 10, d), c)] for c in ("To Do", "In Progress", "Done")]

    assert day(14) == [0, 3, 0]
    assert day(15) == [0, 2, 1]
    assert day(16) == [1, 2, 1]
    assert day(17) == [1, 2, 1]
    assert len(cfd) == 4 * 3
    assert cfd[0] == CfdPoint(date(2026, 10, 14), "To Do", 0)


def test_cycle_time_and_percentiles() -> None:
    metrics = flow_metrics(board(), PLAN, "UTC", window_days=30)

    assert metrics.cycle_times_days == {1: 3.25}
    assert metrics.cycle_time_percentiles == (3.25, 3.25, 3.25)


def test_aging_wip_and_outliers() -> None:
    metrics = flow_metrics(board(), PLAN, "UTC", window_days=30)

    assert metrics.aging_days == {2: 3, 4: 7}
    assert metrics.aging_outliers() == {4: 7.0}


def test_completion_outside_window_is_excluded() -> None:
    assert flow_metrics(board(), date(2026, 12, 31), "UTC", window_days=30).cycle_times_days == {}


def test_no_completions_means_no_percentiles_or_outliers() -> None:
    metrics = flow_metrics(snapshot((task(1),)), PLAN, "UTC", window_days=30)

    assert metrics.cycle_time_percentiles is None
    assert metrics.cycle_time_p85 is None
    assert metrics.aging_outliers() == {}


def test_time_zone_shifts_day_boundaries() -> None:
    # 15th 16:00 UTC is already the 16th in Tokyo, so "Done" appears one day later there.
    utc = {(p.day, p.column): p.count for p in flow_metrics(board(), PLAN, "UTC", 3).cfd}
    tokyo = {(p.day, p.column): p.count for p in flow_metrics(board(), PLAN, "Asia/Tokyo", 3).cfd}

    assert utc[(date(2026, 10, 15), "Done")] == 1
    assert tokyo[(date(2026, 10, 15), "Done")] == 0
    assert tokyo[(date(2026, 10, 16), "Done")] == 1


def test_empty_board() -> None:
    assert flow_metrics(snapshot(()), PLAN, "UTC", 3).cfd == ()


def test_status_categories_prefer_catalogue() -> None:
    categories = status_categories(board())

    assert categories["3"] is StatusCategory.IN_PROGRESS
    assert categories["5"] is StatusCategory.DONE


@pytest.mark.parametrize("window", [0, 1])
def test_window_lengths(window: int) -> None:
    cfd = flow_metrics(board(), PLAN, "UTC", window).cfd

    assert len({p.day for p in cfd}) == window + 1
