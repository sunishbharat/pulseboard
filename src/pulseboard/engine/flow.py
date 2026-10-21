"""Flow metrics from status transitions (DESIGN §10.8): CFD, cycle time, throughput, aging WIP.

Status → column comes from the board configuration; status → category from the snapshot's
status catalogue (falling back to the categories of the issues' current statuses).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from pulseboard.domain.snapshot import BoardSnapshot
from pulseboard.domain.types import IssueId, StatusCategory
from pulseboard.engine.calendar import local_date
from pulseboard.engine.forecaster import PERCENTILES

_SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class CfdPoint:
    """Issues in one column at the end of one day."""

    day: date
    column: str
    count: int


@dataclass(frozen=True)
class FlowMetrics:
    """Flow metrics for one board."""

    cfd: tuple[CfdPoint, ...]
    cycle_times_days: Mapping[IssueId, float]
    """Completed within the window."""
    cycle_time_percentiles: tuple[float, float, float] | None
    """P50, P85, P95 in days; ``None`` without completed items."""
    aging_days: Mapping[IssueId, int]
    """Current age of each in-progress item."""

    @property
    def cycle_time_p85(self) -> float | None:
        """Cycle-time P85 in days."""
        return self.cycle_time_percentiles[1] if self.cycle_time_percentiles else None

    def aging_outliers(self) -> dict[IssueId, float]:
        """In-progress items older than the cycle-time P85 (warning W014)."""
        p85 = self.cycle_time_p85
        if p85 is None:
            return {}
        return {i: float(age) for i, age in sorted(self.aging_days.items()) if age > p85}


def status_categories(snapshot: BoardSnapshot) -> dict[str, StatusCategory]:
    """Status ID → category."""
    categories = {t.status_id: t.status_category for t in snapshot.tasks}
    categories.update({s.status_id: s.category for s in snapshot.statuses})
    return categories


def _end_of_day_utc(day: date, time_zone: str) -> datetime:
    start_next = datetime.combine(day + timedelta(days=1), time(0), ZoneInfo(time_zone))
    return start_next.astimezone(UTC)


def _events(snapshot: BoardSnapshot) -> pd.DataFrame:
    """One row per (issue, time, status), including each issue's creation."""
    first_from: dict[IssueId, str | None] = {}
    for t in snapshot.transitions:  # sorted by issue and time
        first_from.setdefault(t.issue_id, t.from_status_id)
    rows = [
        (task.issue_id, task.created, first_from.get(task.issue_id) or task.status_id)
        for task in snapshot.tasks
    ]
    rows += [(t.issue_id, t.at, t.to_status_id) for t in snapshot.transitions]
    frame = pd.DataFrame(rows, columns=["issue_id", "at", "status_id"])
    frame["at"] = pd.to_datetime(frame["at"], utc=True)
    return frame.sort_values(["at", "issue_id"], kind="stable").reset_index(drop=True)


def cumulative_flow(
    snapshot: BoardSnapshot, plan_date: date, time_zone: str, window_days: int
) -> tuple[CfdPoint, ...]:
    """Daily count of issues per board column over the window ending on the plan date."""
    columns = [str(c.name) for c in snapshot.columns]
    if not snapshot.tasks or not columns:
        return ()
    column_of = {sid: str(c.name) for c in snapshot.columns for sid in c.status_ids}
    days = [plan_date - timedelta(days=n) for n in range(window_days, -1, -1)]
    grid = pd.DataFrame(
        [
            (i, d, _end_of_day_utc(d, time_zone))
            for d in days
            for i in sorted({t.issue_id for t in snapshot.tasks})
        ],
        columns=["issue_id", "day", "at"],
    )
    grid["at"] = pd.to_datetime(grid["at"], utc=True)
    merged = pd.merge_asof(
        grid.sort_values("at", kind="stable"),
        _events(snapshot),
        on="at",
        by="issue_id",
        direction="backward",
        allow_exact_matches=False,
    )
    merged["column"] = merged["status_id"].map(column_of)
    counts = merged.dropna(subset=["column"]).groupby(["day", "column"]).size()
    return tuple(
        CfdPoint(day, column, int(counts.get((day, column), 0)))
        for day in days
        for column in columns
    )


def cycle_times(
    snapshot: BoardSnapshot, plan_date: date, time_zone: str, window_days: int
) -> dict[IssueId, float]:
    """Days from first entry into "in progress" to the last entry into "done", for issues done
    within the window. Issues that never entered "in progress" are left out."""
    category = status_categories(snapshot)
    start: dict[IssueId, datetime] = {}
    finish: dict[IssueId, datetime] = {}
    for t in snapshot.transitions:
        to = category.get(t.to_status_id)
        if to is StatusCategory.IN_PROGRESS:
            start.setdefault(t.issue_id, t.at)
        elif to is StatusCategory.DONE:
            finish[t.issue_id] = t.at
    earliest = plan_date - timedelta(days=window_days)
    result: dict[IssueId, float] = {}
    for task in snapshot.tasks:
        begun, ended = start.get(task.issue_id), finish.get(task.issue_id)
        if task.status_category is not StatusCategory.DONE or begun is None or ended is None:
            continue
        if ended > begun and earliest <= local_date(ended, time_zone) <= plan_date:
            result[task.issue_id] = round((ended - begun).total_seconds() / _SECONDS_PER_DAY, 6)
    return dict(sorted(result.items()))


def aging(snapshot: BoardSnapshot, plan_date: date, time_zone: str) -> dict[IssueId, int]:
    """Whole days each in-progress issue has been in progress, up to the plan date."""
    category = status_categories(snapshot)
    began: dict[IssueId, datetime] = {}
    for t in snapshot.transitions:
        if category.get(t.to_status_id) is StatusCategory.IN_PROGRESS:
            began.setdefault(t.issue_id, t.at)
    return {
        task.issue_id: (plan_date - local_date(began[task.issue_id], time_zone)).days
        for task in snapshot.tasks
        if task.status_category is StatusCategory.IN_PROGRESS and task.issue_id in began
    }


def flow_metrics(
    snapshot: BoardSnapshot, plan_date: date, time_zone: str, window_days: int
) -> FlowMetrics:
    """All flow metrics for one board."""
    cycles = cycle_times(snapshot, plan_date, time_zone, window_days)
    percentiles = None
    if cycles:
        values = pd.Series(list(cycles.values()), dtype=float).quantile(
            [p / 100 for p in PERCENTILES]
        )
        p50, p85, p95 = (round(float(v), 6) for v in values)
        percentiles = (p50, p85, p95)
    return FlowMetrics(
        cfd=cumulative_flow(snapshot, plan_date, time_zone, window_days),
        cycle_times_days=cycles,
        cycle_time_percentiles=percentiles,
        aging_days=aging(snapshot, plan_date, time_zone),
    )
