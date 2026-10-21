"""Effort policy: story points or time estimates to working hours (DESIGN §10.2, ADR-004).

Precedence for an open task:

1. in progress with a remaining estimate → remaining hours (``remaining``);
2. story points → points ÷ points-per-day × hours per day (``story_points``);
3. an original (or, failing that, remaining) time estimate → those hours (``estimate``);
4. nothing → the default effort, counted as unestimated (``default``, warning W003).

Remaining work is preferred for in-progress tasks because it is the only estimate that
reflects work already done.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pulseboard.domain.snapshot import BoardSnapshot, Task
from pulseboard.domain.types import EffortSource, IssueId, SprintState, StatusCategory
from pulseboard.engine.calendar import WorkCalendar, local_date
from pulseboard.settings import PointsMode, PointsPolicy


@dataclass(frozen=True)
class Effort:
    """A task's effort and where it came from."""

    hours: float
    source: EffortSource


def points_per_day(
    snapshot: BoardSnapshot, calendar: WorkCalendar, policy: PointsPolicy, time_zone: str
) -> float:
    """Story points one person completes per working day.

    Fixed mode returns the configured rate. Velocity mode divides the points completed in the
    last ``velocity_sprints`` closed sprints by the person-days worked in them (sprint working
    days × distinct assignees who completed points), falling back when there is no history.
    """
    if policy.mode is PointsMode.FIXED and policy.fixed_points_per_day is not None:
        return policy.fixed_points_per_day
    closed = sorted(
        (
            (s.end, s.sprint_id, s.start)
            for s in snapshot.sprints
            if s.state is SprintState.CLOSED and s.start is not None and s.end is not None
        ),
        reverse=True,
    )[: policy.velocity_sprints]
    points = 0.0
    person_days = 0
    for end, sprint_id, start in closed:
        done = [
            t
            for t in snapshot.tasks
            if t.sprint_id == sprint_id
            and t.status_category is StatusCategory.DONE
            and t.story_points
        ]
        if not done:
            continue
        days = calendar.working_days_inclusive(
            local_date(start, time_zone), local_date(end, time_zone)
        )
        points += sum(t.story_points or 0.0 for t in done)
        person_days += days * len({t.assignee for t in done})
    if points <= 0 or person_days <= 0:
        return policy.fallback_points_per_day
    return points / person_days


def effort_for(
    task: Task, *, points_per_day: float, hours_per_day: float, default_effort_hours: float
) -> Effort:
    """The effort for one open task; see the module docstring for precedence."""
    in_progress = task.status_category is StatusCategory.IN_PROGRESS
    if in_progress and task.remaining_estimate_hours is not None:
        return Effort(task.remaining_estimate_hours, EffortSource.REMAINING)
    if task.story_points is not None:
        return Effort(task.story_points / points_per_day * hours_per_day, EffortSource.STORY_POINTS)
    estimate = task.original_estimate_hours
    if estimate is None:
        estimate = task.remaining_estimate_hours
    if estimate is not None:
        return Effort(estimate, EffortSource.ESTIMATE)
    return Effort(default_effort_hours, EffortSource.DEFAULT)


def estimated_share(efforts: Mapping[IssueId, Effort]) -> float:
    """Share of open tasks with a real estimate (1.0 when there are none, DESIGN §3.1)."""
    if not efforts:
        return 1.0
    estimated = sum(1 for e in efforts.values() if e.source is not EffortSource.DEFAULT)
    return estimated / len(efforts)


def open_tasks(tasks: Sequence[Task]) -> tuple[Task, ...]:
    """Tasks that still need work."""
    return tuple(t for t in tasks if t.status_category is not StatusCategory.DONE)
