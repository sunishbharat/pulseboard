"""Run the engine's scheduling stages on test snapshots."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from factories import PLAN_DATE
from pulseboard.domain.snapshot import BoardSnapshot
from pulseboard.domain.types import IssueId
from pulseboard.engine.calendar import WorkCalendar, calendar_from_settings
from pulseboard.engine.effort import Effort, effort_for, open_tasks
from pulseboard.engine.pyjobshop_scheduler import PyJobShopScheduler
from pulseboard.engine.request_builder import BuiltRequest, build_request
from pulseboard.engine.scheduling import ScheduleResult, TaskTiming
from pulseboard.settings import TeamSettings

POINTS_PER_DAY = 1.0


@dataclass(frozen=True)
class Scheduled:
    """Everything a scheduling test may inspect."""

    result: ScheduleResult
    built: BuiltRequest
    calendar: WorkCalendar
    efforts: dict[IssueId, Effort]

    def timing(self, issue_id: int) -> TaskTiming:
        """The timing of one issue."""
        return next(t for t in self.result.timings if t.issue_id == issue_id)

    def span(self, issue_id: int) -> tuple[int, int]:
        """(start, end) units of one issue."""
        t = self.timing(issue_id)
        return (t.start, t.end)


def efforts_for(snapshots: Sequence[BoardSnapshot], team: TeamSettings) -> dict[IssueId, Effort]:
    """Effort for every open task, at a fixed points-per-day rate."""
    return {
        t.issue_id: effort_for(
            t,
            points_per_day=POINTS_PER_DAY,
            hours_per_day=team.hours_per_day,
            default_effort_hours=team.default_effort_hours,
        )
        for s in snapshots
        for t in open_tasks(s.tasks)
    }


def schedule(
    *snapshots: BoardSnapshot,
    team: TeamSettings | None = None,
    plan_date: date = PLAN_DATE,
) -> Scheduled:
    """Calendar → efforts → request → solve."""
    team = team or TeamSettings()
    calendar = calendar_from_settings(plan_date, team, horizon_years=1)
    efforts = efforts_for(snapshots, team)
    built = build_request(snapshots, efforts, calendar, team)
    result = PyJobShopScheduler().solve(built.request)
    return Scheduled(result=result, built=built, calendar=calendar, efforts=efforts)
