"""Workload and capacity per person-week (DESIGN §3, workload report; warning W008).

A task's effort is spread evenly over the units it occupies, excluding the person's breaks.
Available time per unit is the person's capacity × half a working day.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from pulseboard.domain.plan import ScheduledTask
from pulseboard.domain.types import AccountId, IssueId
from pulseboard.engine.calendar import HALF_DAYS_PER_DAY, Break, WorkCalendar
from pulseboard.engine.effort import Effort
from pulseboard.engine.scheduling import TaskTiming


@dataclass(frozen=True)
class WeekLoad:
    """One person's load in one week (weeks start on Monday)."""

    account_id: AccountId
    week_start: date
    allocated_hours: float
    available_hours: float

    @property
    def utilization(self) -> float:
        """Allocated ÷ available; 0 when nothing is available."""
        return self.allocated_hours / self.available_hours if self.available_hours else 0.0


def _in_break(unit: int, breaks: tuple[Break, ...]) -> bool:
    return any(start <= unit < end for start, end in breaks)


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def workload(
    timings: Mapping[IssueId, TaskTiming],
    efforts: Mapping[IssueId, Effort],
    calendar: WorkCalendar,
    hours_per_day: float,
) -> tuple[WeekLoad, ...]:
    """Load per person and week, over the weeks each person has work; sorted."""
    hours_per_unit = hours_per_day / HALF_DAYS_PER_DAY
    allocated: dict[tuple[AccountId, date], float] = defaultdict(float)
    span: dict[AccountId, tuple[int, int]] = {}
    for issue_id, timing in timings.items():
        account = timing.resource
        if account is None:
            continue
        breaks = calendar.breaks(account)
        units = [u for u in range(timing.start, timing.end) if not _in_break(u, breaks)]
        if not units:
            continue
        share = efforts[issue_id].hours / len(units)
        for unit in units:
            allocated[(account, _week_start(calendar.date_of_unit(unit)))] += share
        first, last = span.get(account, (timing.start, timing.end))
        span[account] = (min(first, timing.start), max(last, timing.end))

    available: dict[tuple[AccountId, date], float] = defaultdict(float)
    for account, (first, last) in span.items():
        capacity = calendar.person(account).capacity
        breaks = calendar.breaks(account)
        for unit in range(first, last):
            if not _in_break(unit, breaks):
                week = _week_start(calendar.date_of_unit(unit))
                available[(account, week)] += capacity * hours_per_unit

    keys = sorted(set(allocated) | set(available))
    return tuple(
        WeekLoad(a, w, round(allocated[(a, w)], 6), round(available[(a, w)], 6)) for a, w in keys
    )


def plan_workload(
    tasks: Sequence[ScheduledTask], calendar: WorkCalendar, hours_per_day: float
) -> tuple[WeekLoad, ...]:
    """Workload from a stored plan's scheduled tasks (whole working days, no solver state)."""
    timings = {
        t.issue_id: TaskTiming(
            issue_id=t.issue_id,
            start=calendar.unit_of_date(t.start),
            end=calendar.unit_of_date(t.end) + HALF_DAYS_PER_DAY,
            resource=t.lane,
        )
        for t in tasks
    }
    efforts = {t.issue_id: Effort(t.effort_hours, t.effort_source) for t in tasks}
    return workload(timings, efforts, calendar, hours_per_day)
