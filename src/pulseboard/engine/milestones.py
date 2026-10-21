"""Milestone forecasts: fix versions with release dates and epics with due dates (ADR-008).

A milestone's forecast is the latest solved end of its open linked issues. A milestone whose
linked issues are all done has no forecast. Slip is in team working days, positive when late.
"""

from collections.abc import Mapping, Sequence
from datetime import date

from pulseboard.domain.plan import MilestoneForecast, MilestoneKind, ScheduledTask
from pulseboard.domain.snapshot import BoardSnapshot, Task
from pulseboard.domain.types import IssueId
from pulseboard.domain.untrusted import UntrustedText
from pulseboard.engine.calendar import WorkCalendar


def _forecast(linked: Sequence[Task], scheduled: Mapping[IssueId, ScheduledTask]) -> date | None:
    ends = [scheduled[t.issue_id].end for t in linked if t.issue_id in scheduled]
    return max(ends, default=None)


def _milestone(
    kind: MilestoneKind,
    milestone_id: int,
    name: UntrustedText,
    target: date | None,
    forecast: date | None,
    calendar: WorkCalendar,
) -> MilestoneForecast:
    slip = calendar.working_days_between(target, forecast) if target and forecast else None
    return MilestoneForecast(
        kind=kind,
        milestone_id=milestone_id,
        name=name,
        target_date=target,
        forecast_date=forecast,
        slip_working_days=slip,
    )


def milestone_forecasts(
    snapshots: Sequence[BoardSnapshot],
    scheduled: Mapping[IssueId, ScheduledTask],
    calendar: WorkCalendar,
) -> tuple[MilestoneForecast, ...]:
    """Every milestone on the board(s), ordered by target date, kind and ID."""
    tasks = {t.issue_id: t for snap in snapshots for t in snap.tasks}
    versions = {v.version_id: v for snap in snapshots for v in snap.versions}
    epics = {e.issue_id: e for snap in snapshots for e in snap.epics}
    found: list[MilestoneForecast] = []
    for version in versions.values():
        if version.release_date is None or version.released:
            continue
        linked = [t for t in tasks.values() if version.version_id in t.fix_version_ids]
        found.append(
            _milestone(
                MilestoneKind.FIX_VERSION,
                version.version_id,
                version.name,
                version.release_date,
                _forecast(linked, scheduled),
                calendar,
            )
        )
    for epic in epics.values():
        if epic.due_date is None or epic.done:
            continue
        linked = [t for t in tasks.values() if t.epic_issue_id == epic.issue_id]
        found.append(
            _milestone(
                MilestoneKind.EPIC,
                epic.issue_id,
                epic.name,
                epic.due_date,
                _forecast(linked, scheduled),
                calendar,
            )
        )
    return tuple(
        sorted(found, key=lambda m: (m.target_date or date.max, m.kind.value, m.milestone_id))
    )
