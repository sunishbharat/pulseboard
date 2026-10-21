"""Effort policy (DESIGN §10.2, ADR-004)."""

from datetime import UTC, date, datetime

import pytest

from factories import PLAN_DATE, snapshot, task
from pulseboard.domain.snapshot import Sprint
from pulseboard.domain.types import EffortSource, IssueId, SprintId, SprintState, StatusCategory
from pulseboard.domain.untrusted import UntrustedText
from pulseboard.engine.calendar import WorkCalendar
from pulseboard.engine.effort import (
    Effort,
    effort_for,
    estimated_share,
    open_tasks,
    points_per_day,
)
from pulseboard.settings import PointsMode, PointsPolicy

CAL = WorkCalendar(
    plan_date=PLAN_DATE, team_holiday_country="US", hours_per_day=8.0, horizon_years=1
)


def effort(category: StatusCategory = StatusCategory.TO_DO, **changes: float) -> Effort:
    return effort_for(
        task(1, category=category, **changes),
        points_per_day=2.0,
        hours_per_day=8.0,
        default_effort_hours=6.0,
    )


def test_story_points() -> None:
    assert effort(story_points=3.0) == Effort(12.0, EffortSource.STORY_POINTS)


def test_time_estimate() -> None:
    assert effort(original_estimate_hours=5.0) == Effort(5.0, EffortSource.ESTIMATE)
    assert effort(remaining_estimate_hours=7.0) == Effort(7.0, EffortSource.ESTIMATE)


def test_in_progress_uses_remaining_estimate() -> None:
    e = effort(
        category=StatusCategory.IN_PROGRESS,
        story_points=8.0,
        original_estimate_hours=20.0,
        remaining_estimate_hours=3.0,
    )
    assert e == Effort(3.0, EffortSource.REMAINING)


def test_unestimated_uses_default() -> None:
    assert effort() == Effort(6.0, EffortSource.DEFAULT)


def test_zero_points_is_an_estimate() -> None:
    assert effort(story_points=0.0) == Effort(0.0, EffortSource.STORY_POINTS)


def test_estimated_share() -> None:
    efforts = {
        IssueId(1): Effort(1.0, EffortSource.STORY_POINTS),
        IssueId(2): Effort(1.0, EffortSource.DEFAULT),
        IssueId(3): Effort(1.0, EffortSource.ESTIMATE),
        IssueId(4): Effort(1.0, EffortSource.DEFAULT),
        IssueId(5): Effort(1.0, EffortSource.REMAINING),
    }
    assert estimated_share(efforts) == pytest.approx(0.6)
    assert estimated_share({}) == 1.0


def sprint(
    sprint_id: int, start: date, end: date, state: SprintState = SprintState.CLOSED
) -> Sprint:
    return Sprint(
        sprint_id=SprintId(sprint_id),
        name=UntrustedText(f"Sprint {sprint_id}"),
        state=state,
        start=datetime(start.year, start.month, start.day, 9, tzinfo=UTC),
        end=datetime(end.year, end.month, end.day, 17, tzinfo=UTC),
    )


def test_velocity_from_last_closed_sprints() -> None:
    # Sprint 2: Oct 5-16 = 9 working days (Oct 12 is Columbus Day in the US calendar);
    # alice + bob complete 30 points -> 30 / (9 x 2) person-days.
    # Sprint 1 is older than velocity_sprints=1 and ignored.
    done = StatusCategory.DONE
    board = snapshot(
        (
            task(1, category=done, sprint_id=SprintId(1), story_points=100.0),
            task(2, category=done, sprint_id=SprintId(2), story_points=20.0),
            task(3, category=done, sprint_id=SprintId(2), story_points=10.0, assignee="bob"),
            task(4, sprint_id=SprintId(2), story_points=50.0),  # not done: ignored
        ),
        sprints=(
            sprint(1, date(2026, 9, 21), date(2026, 10, 2)),
            sprint(2, date(2026, 10, 5), date(2026, 10, 16)),
            sprint(3, date(2026, 10, 19), date(2026, 10, 30), SprintState.ACTIVE),
        ),
    )

    rate = points_per_day(board, CAL, PointsPolicy(velocity_sprints=1), "UTC")

    assert rate == pytest.approx(30 / 18)


def test_velocity_falls_back_without_history() -> None:
    policy = PointsPolicy(fallback_points_per_day=0.75)

    assert points_per_day(snapshot(), CAL, policy, "UTC") == 0.75


def test_fixed_rate() -> None:
    policy = PointsPolicy(mode=PointsMode.FIXED, fixed_points_per_day=2.5)

    assert points_per_day(snapshot(), CAL, policy, "UTC") == 2.5


def test_open_tasks() -> None:
    tasks = (
        task(1),
        task(2, category=StatusCategory.DONE),
        task(3, category=StatusCategory.IN_PROGRESS),
    )

    assert [t.issue_id for t in open_tasks(tasks)] == [1, 3]
