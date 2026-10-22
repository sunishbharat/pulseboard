"""The ``ReportDefinition`` protocol, its context and result, chart options and the registry.

A report computes tables from a stored plan and its snapshots (no Jira calls, no LLM) and maps
them to a Plotly figure. Reports register in the ``pulseboard.reports`` entry-point group.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from importlib.metadata import entry_points
from typing import Protocol

import plotly.graph_objects as go

from pulseboard.domain.plan import Plan
from pulseboard.domain.snapshot import BoardSnapshot, Task
from pulseboard.domain.types import AccountId, IssueId, ReportId
from pulseboard.engine.calendar import WorkCalendar, calendar_from_settings
from pulseboard.engine.flow import FlowMetrics, flow_metrics
from pulseboard.engine.forecaster import Throughput, weekly_throughput
from pulseboard.engine.workload import WeekLoad, plan_workload
from pulseboard.reports.theme import ThemeMode, template_name
from pulseboard.settings import Settings

ENTRY_POINT_GROUP = "pulseboard.reports"

Cell = str | int | float | date | None


@dataclass(frozen=True)
class Table:
    """A named table: column names and rows of plain values."""

    name: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Cell, ...], ...]

    def column(self, name: str) -> list[Cell]:
        """All values of one column."""
        index = self.columns.index(name)
        return [row[index] for row in self.rows]


@dataclass(frozen=True)
class ReportResult:
    """Tables, an optional figure and a short summary of one report."""

    report_id: ReportId
    title: str
    tables: tuple[Table, ...]
    figure: go.Figure | None
    summary: tuple[str, ...] = ()
    """At most three lines, for ``pb_render``."""
    dark_figure: go.Figure | None = None
    """The same figure in the dark theme, for HTML pages that follow the viewer's color scheme."""
    titled_figure: bool = False
    """The figure carries its own report header, so pages need not repeat it."""


class GroupBy(StrEnum):
    """Gantt summary rows (Addendum 02 §6.4)."""

    EPIC = "epic"
    ASSIGNEE = "assignee"
    NONE = "none"


class ColorBy(StrEnum):
    """What the Gantt task-bar color encodes (Addendum 02 §5)."""

    TYPE = "type"
    """Planned / critical / summary, the project-tool convention."""
    EPIC = "epic"


@dataclass(frozen=True)
class ChartOptions:
    """How figures are drawn; filters change what is shown, never the schedule (§6.6)."""

    theme: ThemeMode = ThemeMode.LIGHT
    group_by: GroupBy = GroupBy.EPIC
    color_by: ColorBy = ColorBy.TYPE
    patterns: bool = False
    """Line pattern on critical bars (accessibility and print setting)."""
    collapsed: bool | None = None
    """Show summary rows only; ``None`` collapses plans of more than 150 tasks (§8)."""
    assignees: tuple[AccountId | None, ...] = ()
    """Show only these assignees (``None`` = unassigned); empty shows everyone."""
    epics: tuple[IssueId | None, ...] = ()
    """Show only these epics (``None`` = no epic); empty shows every epic."""
    critical_only: bool = False

    @property
    def filtered(self) -> bool:
        """Whether any filter hides tasks."""
        return bool(self.assignees or self.epics or self.critical_only)


@dataclass(frozen=True)
class ReportContext:
    """Everything reports may read: the plan, its snapshots and derived engine results."""

    plan: Plan
    snapshots: tuple[BoardSnapshot, ...]
    settings: Settings
    calendar: WorkCalendar
    flow: FlowMetrics | None
    throughput: Throughput
    workload: tuple[WeekLoad, ...]
    tasks: Mapping[IssueId, Task] = field(default_factory=dict)
    """Every task on the snapshots, open or done."""
    chart: ChartOptions = ChartOptions()

    @property
    def time_zone(self) -> str:
        """The team time zone."""
        return self.settings.team.time_zone


def build_context(
    plan: Plan,
    snapshots: Sequence[BoardSnapshot],
    settings: Settings,
    chart: ChartOptions = ChartOptions(),  # noqa: B008 - frozen, so sharing is safe
) -> ReportContext:
    """Derive the shared engine results once for all reports of a plan."""
    team, limits = settings.team, settings.limits
    people = tuple({p.account_id: p for s in snapshots for p in s.people}.values())
    calendar = calendar_from_settings(plan.plan_date, team, limits.planning_horizon_years, people)
    tasks = {t.issue_id: t for s in snapshots for t in s.tasks}
    flow = (
        flow_metrics(snapshots[0], plan.plan_date, team.time_zone, limits.reporting_window_days)
        if snapshots
        else None
    )
    throughput = weekly_throughput(
        tuple(tasks.values()),
        plan.plan_date,
        team.time_zone,
        limits.throughput_history_weeks,
        limits.reporting_window_days,
    )
    return ReportContext(
        plan=plan,
        snapshots=tuple(snapshots),
        settings=settings,
        calendar=calendar,
        flow=flow,
        throughput=throughput,
        workload=plan_workload(plan.scheduled_tasks, calendar, team.hours_per_day),
        tasks=tasks,
        chart=chart,
    )


class ReportDefinition(Protocol):
    """A report plugin."""

    @property
    def report_id(self) -> ReportId:
        """Stable report ID."""
        ...

    @property
    def title(self) -> str:
        """Human title."""
        ...

    def build(self, context: ReportContext) -> ReportResult:
        """Compute tables and the figure."""
        ...


@dataclass(frozen=True)
class Report:
    """A report defined by a build function."""

    report_id: ReportId
    title: str
    release: str
    builder: Callable[[ReportContext], ReportResult]

    def build(self, context: ReportContext) -> ReportResult:
        """Compute tables and the figure."""
        return self.builder(context)


def load_reports() -> dict[ReportId, ReportDefinition]:
    """Every report registered in the ``pulseboard.reports`` entry-point group."""
    reports: dict[ReportId, ReportDefinition] = {}
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        report = entry.load()
        reports[report.report_id] = report
    return dict(sorted(reports.items()))


def base_layout(figure: go.Figure, title: str, theme: ThemeMode = ThemeMode.LIGHT) -> go.Figure:
    """Consistent layout for every chart, in the Pulseboard theme."""
    figure.update_layout(
        title={"text": title},
        template=template_name(theme),
        margin={"l": 60, "r": 30, "t": 60, "b": 40},
        legend={"orientation": "h", "y": -0.15},
    )
    return figure
