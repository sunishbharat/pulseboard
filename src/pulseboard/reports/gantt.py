"""The Gantt chart: a project-tool split view built from Plotly subplots (Addendum 02, ADR-028).

Left, the task table (§6.1): one text trace per column. Right, the timeline: task and progress
bars, summary brackets, milestones, link lines, a two-tier timescale, nonworking time and today.
Both subplots share one numeric y-axis (row ``i`` at ``y = i``), so rows align exactly.
"""

import html
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from pulseboard.domain.plan import MilestoneForecast, MilestoneKind, ScheduledTask
from pulseboard.domain.snapshot import Task
from pulseboard.domain.types import (
    AccountId,
    BoardType,
    ConstraintReason,
    Deployment,
    EffortSource,
    ForecastMethod,
    IssueId,
    SprintState,
    StatusCategory,
)
from pulseboard.engine.calendar import local_date
from pulseboard.reports.base import ColorBy, GroupBy, ReportContext
from pulseboard.reports.theme import PALETTES, Palette, ThemeMode, template_name

ROW_PX = 24
TIER_PX = 20
MARK_PX = 14
"""Row under the tiers for the Today and forecast-marker labels."""
SPRINT_PX = 18
LEGEND_PX = 44
TABLE_SHARE = 0.45
"""Share of the figure width taken by the task table (§4)."""
REFERENCE_WIDTH = 1200
"""Figure width in a 1280 px window, the narrowest supported; column text is truncated to fit."""
CHAR_PX = 6.6
"""Average width of a 12 px system-UI character."""
COLLAPSE_ABOVE = 150
MANY_PEOPLE = 20
BAR, PROGRESS, SUMMARY = 0.6, 0.18, 0.2
"""Bar thickness as a share of the row height (§6.3, §6.4)."""
DAY_MS = 86_400_000
EPOCH = date(1970, 1, 1)
UNASSIGNED, NO_EPIC = "Unassigned", "No epic"
OPEN = "rgba(0,0,0,0)"
"""Fill of open paths (link lines, sprint brackets)."""
TABLE_COLUMNS: tuple[tuple[str, int], ...] = (
    ("ID", 36),
    ("Task name", 0),
    ("Dur.", 40),
    ("Start", 50),
    ("Finish", 50),
    ("Pred.", 60),
    ("Resource", 84),
)
"""Column headers and widths in pixels at the reference width; "Task name" takes the rest."""
PRED_MAX_PX = 180
"""The Pred. column widens to fit its longest cell, up to this; "Task name" gives way."""
YEAR_PX = 16
"""Extra width of the Start and Finish columns when dates show the year."""
REASONS = {
    ConstraintReason.PLAN_DATE: "Ready now; starts on the first working day",
    ConstraintReason.RELEASE_DATE: "Can't start before its sprint or release date",
}
DEPLOYMENTS = {Deployment.CLOUD: "Jira Cloud", Deployment.DATA_CENTER: "Jira Data Center"}


class RowKind(StrEnum):
    """What a Gantt row shows."""

    SUMMARY = "summary"
    TASK = "task"
    MILESTONE = "milestone"


@dataclass
class Row:
    """One table row and its bar, before layout."""

    kind: RowKind
    name: str
    start: date
    end: date
    """Inclusive."""
    critical: bool = False
    progress: float = 0.0
    effort: float = 0.0
    resource: str = ""
    task: ScheduledTask | None = None
    source: Task | None = None
    milestone: MilestoneForecast | None = None
    predecessors: tuple[IssueId, ...] = ()
    children: int = 0
    members: tuple[IssueId, ...] = ()
    """A summary row's tasks; a collapsed group stands in for them in link lines."""
    number: int = 0
    labels: list[str] = field(default_factory=list)


def _ms(day: date) -> int:
    return (day - EPOCH).days * DAY_MS


def _stamp(ms: int) -> str:
    moment = datetime.combine(EPOCH, time()) + timedelta(milliseconds=ms)
    return moment.isoformat(sep=" ")


def _iso(day: date) -> str:
    return day.isoformat()


def _short(day: date, years: bool) -> str:
    return f"{day:%b} {day.day}" + (f" '{day:%y}" if years else "")


def _days(value: float) -> str:
    return f"{value:g}d"


def _fit(text: str, chars: int) -> str:
    return text if len(text) <= chars else text[: max(chars - 1, 1)] + "…"


def _fit_tokens(tokens: Sequence[str], chars: int) -> str:
    """Comma-joined identifiers that fit in ``chars``; never cut inside one (ADDENDUM-03 §12).
    What does not fit is counted: ``ABC-7,XYZ-9 (ext) +2``."""
    for shown in range(len(tokens), 0, -1):
        rest = len(tokens) - shown
        text = ",".join(tokens[:shown]) + (f" +{rest}" if rest else "")
        if len(text) <= chars:
            return text
    return f"+{len(tokens)}" if tokens else ""


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


# ---------------------------------------------------------------- rows


def _progress(source: Task | None) -> tuple[float, str]:
    """Completed share and how it was derived (§6.3)."""
    if source and source.original_estimate_hours and source.remaining_estimate_hours is not None:
        done = 1 - source.remaining_estimate_hours / source.original_estimate_hours
        return min(max(done, 0.0), 1.0), "remaining vs. original estimate"
    category = source.status_category if source else StatusCategory.TO_DO
    share = {StatusCategory.TO_DO: 0.0, StatusCategory.IN_PROGRESS: 0.5}.get(category, 1.0)
    return share, "from status"


def visible_tasks(ctx: ReportContext, tasks: Sequence[ScheduledTask]) -> list[ScheduledTask]:
    """The tasks the chart's filters let through (§6.6); all of them without filters."""
    options = ctx.chart

    def epic(t: ScheduledTask) -> IssueId | None:
        source = ctx.tasks.get(t.issue_id)
        return source.epic_issue_id if source else None

    return [
        t
        for t in tasks
        if (not options.assignees or t.lane in options.assignees)
        and (not options.epics or epic(t) in options.epics)
        and (not options.critical_only or t.critical)
    ]


@dataclass(frozen=True)
class Names:
    """Display names of people and epics."""

    people: dict[AccountId, str]
    epics: dict[IssueId, str]

    def person(self, account_id: AccountId | None) -> str:
        return self.people.get(account_id, account_id) if account_id else UNASSIGNED

    def epic(self, issue_id: IssueId | None) -> str:
        return self.epics.get(issue_id, f"Epic {issue_id}") if issue_id is not None else NO_EPIC


def _names(ctx: ReportContext) -> Names:
    people = {p.account_id: str(p.display_name) for s in ctx.snapshots for p in s.people}
    epics = {e.issue_id: f"{e.issue_key} {e.name}" for s in ctx.snapshots for e in s.epics}
    return Names(people, epics)


def _task_row(ctx: ReportContext, t: ScheduledTask, names: Names, by_epic: bool) -> Row:
    source = ctx.tasks.get(t.issue_id)
    progress, _ = _progress(source)
    preds = tuple(e.predecessor for e in ctx.plan.dependency_edges if e.successor == t.issue_id)
    resource = names.person(t.lane) + ("?" if t.tentative and t.lane else "")
    return Row(
        RowKind.TASK,
        str(source.summary) if source else "",
        t.start,
        t.end,
        critical=t.critical,
        progress=progress,
        effort=t.effort_hours,
        resource=resource if by_epic or ctx.chart.group_by is not GroupBy.ASSIGNEE else t.issue_key,
        task=t,
        source=source,
        predecessors=tuple(sorted(preds)),
    )


def _milestone_rows(
    ctx: ReportContext, visible: Sequence[ScheduledTask]
) -> list[tuple[IssueId | None, Row]]:
    """Each dated milestone with linked visible work, keyed by its last-finishing task."""
    rows: list[tuple[IssueId | None, Row]] = []
    for m in ctx.plan.milestones:
        if m.forecast_date is None:
            continue
        linked = [
            t
            for t in ctx.plan.scheduled_tasks
            if (source := ctx.tasks.get(t.issue_id))
            and (
                m.milestone_id in source.fix_version_ids
                if m.kind is MilestoneKind.FIX_VERSION
                else source.epic_issue_id == m.milestone_id
            )
        ]
        if not any(t in visible for t in linked):
            continue
        last = max((t.end for t in linked), default=m.forecast_date)
        preds = tuple(sorted(t.issue_id for t in linked if t.end == last))
        row = Row(
            RowKind.MILESTONE,
            str(m.name),
            m.forecast_date,
            m.forecast_date,
            critical=any(t.critical for t in linked if t.end == last),
            milestone=m,
            predecessors=preds,
        )
        rows.append((preds[0] if preds else None, row))
    return rows


def _summary(name: str, children: Sequence[Row], ctx: ReportContext) -> Row:
    start, end = min(r.start for r in children), max(r.end for r in children)
    effort = sum(r.effort for r in children)
    done = sum(r.progress * r.effort for r in children)
    return Row(
        RowKind.SUMMARY,
        name + (" (filtered)" if ctx.chart.filtered else ""),
        start,
        end,
        critical=any(r.critical for r in children),
        progress=done / effort if effort else 0.0,
        effort=effort,
        children=sum(1 for r in children if r.kind is RowKind.TASK),
        members=tuple(r.task.issue_id for r in children if r.task),
    )


def build_rows(ctx: ReportContext, tasks: Sequence[ScheduledTask]) -> tuple[list[Row], bool]:
    """The ordered rows (§6.4) and whether groups are collapsed (§8)."""
    options, names = ctx.chart, _names(ctx)
    visible = visible_tasks(ctx, tasks)
    task_rows = {
        t.issue_id: _task_row(ctx, t, names, options.group_by is GroupBy.EPIC) for t in visible
    }
    milestones = _milestone_rows(ctx, visible)
    order = sorted(
        task_rows.values(),
        key=lambda r: (
            r.start,
            r.source.board_rank if r.source else 0,
            r.task.issue_key if r.task else "",
        ),
    )
    if options.group_by is GroupBy.NONE:
        rows = order + [r for _, r in milestones]
        rows.sort(key=lambda r: (r.start, r.kind is RowKind.MILESTONE))
        return _numbered(rows), False

    groups: dict[str, list[Row]] = {}
    for row in order:
        source, task = row.source, row.task
        if options.group_by is GroupBy.EPIC:
            key = names.epic(source.epic_issue_id if source else None)
        else:
            key = names.person(task.lane if task else None)
        groups.setdefault(key, []).append(row)
    owner = {r.task.issue_id: key for key, rs in groups.items() for r in rs if r.task}
    orphans = []
    for last, milestone in milestones:
        if last in owner:
            groups[owner[last]].append(milestone)
        else:
            orphans.append(milestone)
    summaries = sorted(
        ((_summary(name, rs, ctx), rs) for name, rs in groups.items()),
        key=lambda pair: (pair[0].start, pair[0].name),
    )
    collapsed = (
        options.collapsed if options.collapsed is not None else len(visible) > COLLAPSE_ABOVE
    )
    rows = []
    for summary, children in summaries:
        rows.append(summary)
        if not collapsed:
            rows.extend(children)
    trailing = [r for _, r in milestones] if collapsed else orphans
    rows.extend(sorted(trailing, key=lambda r: r.start))
    return _numbered(rows), collapsed


def _numbered(rows: list[Row]) -> list[Row]:
    for number, row in enumerate(rows, start=1):
        row.number = number
    return rows


# ---------------------------------------------------------------- timescale


@dataclass(frozen=True)
class Tiers:
    """Tick positions and labels of the two timescale tiers for one visible range (§6.2)."""

    top_labels: tuple[tuple[str, str], ...]
    bottom_labels: tuple[tuple[str, str], ...]
    top_bounds: tuple[str, ...]
    bottom_bounds: tuple[str, ...]


def _periods(start: date, end: date, step: str) -> list[tuple[date, date, str]]:
    """Calendar periods ``[a, b)`` covering ``[start, end)``, with labels."""
    periods: list[tuple[date, date, str]] = []
    if step == "day":
        day = start
        while day < end:
            periods.append((day, day + timedelta(days=1), "MTWTFSS"[day.weekday()]))
            day += timedelta(days=1)
        return periods
    if step == "week":
        day = _monday(start)
        while day < end:
            iso = day.isocalendar().week
            label = f"{day:%b} {day.day} · Wk {iso}"
            periods.append((day, day + timedelta(days=7), label))
            day += timedelta(days=7)
        return periods
    months = 3 if step == "quarter" else 1
    day = date(start.year, (start.month - 1) // months * months + 1, 1)
    while day < end:
        month = day.month - 1 + months
        nxt = date(day.year + month // 12, month % 12 + 1, 1)
        if step == "quarter":
            label = f"Q{(day.month - 1) // 3 + 1} {day.year}"
        elif step == "month_year":
            label = f"{day:%b} {day.year}"
        else:
            label = f"{day:%b}"
        periods.append((day, nxt, label))
        day = nxt
    return periods


def tiers_for(start: date, end: date) -> Tiers:
    """Week/day up to 3 weeks, month/week up to 4 months, else quarter/month (§6.2)."""
    span = (end - start).days
    if span <= 21:
        top, bottom = _periods(start, end, "week"), _periods(start, end, "day")
    elif span <= 122:
        top = _periods(start, end, "month_year")
        bottom = [(a, b, str(a.day)) for a, b, _ in _periods(start, end, "week")]
    else:
        top, bottom = _periods(start, end, "quarter"), _periods(start, end, "month")

    def labels(periods: list[tuple[date, date, str]]) -> tuple[tuple[str, str], ...]:
        out = []
        for a, b, text in periods:
            lo, hi = max(a, start), min(b, end)
            if hi > lo:
                mid = datetime.combine(lo, time()) + (hi - lo) / 2
                out.append((mid.isoformat(sep=" "), text))
        return tuple(out)

    def bounds(periods: list[tuple[date, date, str]]) -> tuple[str, ...]:
        return tuple(_iso(a) for a, _, _ in periods if start < a < end)

    return Tiers(labels(top), labels(bottom), bounds(top), bounds(bottom))


def _tier_axes(tiers: Tiers) -> dict[str, object]:
    """Relayout arguments that put ``tiers`` on the timescale axes."""
    return {
        "xaxis2.tickvals": [x for x, _ in tiers.bottom_labels],
        "xaxis2.ticktext": [t for _, t in tiers.bottom_labels],
        "xaxis3.tickvals": [x for x, _ in tiers.top_labels],
        "xaxis3.ticktext": [t for _, t in tiers.top_labels],
        "xaxis4.tickvals": list(tiers.bottom_bounds),
        "xaxis5.tickvals": list(tiers.top_bounds),
    }


# ---------------------------------------------------------------- link lines


def link_path(
    finish_ms: int, row_from: int, start_ms: int, row_to: int, stub_ms: int
) -> list[tuple[int, float]]:
    """Right-angle finish-to-start route (§6.5).

    The line leaves at the predecessor's finish edge, so it never crosses the resource label
    to the right of the bar. With room before the successor it runs down in the gap and into
    the successor's start; otherwise it drops below the predecessor and enters from the left.
    """
    side = 1 if row_to >= row_from else -1
    if start_ms - finish_ms >= stub_ms:
        return [(finish_ms, row_from), (finish_ms, row_to), (start_ms, row_to)]
    back, between = start_ms - stub_ms, row_from + side * 0.5
    return [
        (finish_ms, row_from),
        (finish_ms, between),
        (back, between),
        (back, row_to),
        (start_ms, row_to),
    ]


@dataclass(frozen=True)
class Link:
    """One link line: out of ``before`` (``None``: a hidden predecessor) into ``row``."""

    predecessor: IssueId
    before: Row | None
    row: Row
    finish_ms: int
    """The end of the predecessor's last day."""
    start_ms: int
    """The start of the successor's first day (a milestone: the end of its day)."""
    critical: bool


def link_list(ctx: ReportContext, rows: Sequence[Row]) -> tuple[list[Link], int]:
    """The link lines of the shown rows (§6.5), and how many dependencies they stand for.

    A collapsed group stands in for its tasks: a link between two groups runs from the
    predecessor's finish on its group row into the successor's start on the other, one line
    per pair of rows (critical first, then the latest finish). Links inside one collapsed
    group are not drawn. A predecessor on no shown row gets a "hidden" stub.
    """
    tasks = {t.issue_id: t for t in ctx.plan.scheduled_tasks}
    anchor = {r.task.issue_id: r for r in rows if r.task}
    for r in rows:
        for member in r.members:
            anchor.setdefault(member, r)  # only when the group is collapsed
    preds_of: dict[IssueId, list[IssueId]] = {}
    for e in ctx.plan.dependency_edges:
        preds_of.setdefault(e.successor, []).append(e.predecessor)

    wanted: list[tuple[IssueId, bool, Row, int]] = []
    for row in rows:
        if row.kind is RowKind.MILESTONE:
            start = _ms(row.start + timedelta(days=1))
            wanted += [(p, row.critical, row, start) for p in row.predecessors]
        elif row.task:
            wanted += [(p, row.critical, row, _ms(row.start)) for p in row.predecessors]
        else:
            for member in row.members:
                if anchor[member] is row:
                    t = tasks[member]
                    wanted += [
                        (p, t.critical, row, _ms(t.start)) for p in sorted(preds_of.get(member, ()))
                    ]

    best: dict[tuple[int, int, int], Link] = {}
    for pred, successor_critical, row, start in wanted:
        before = anchor.get(pred)
        if before is row or pred not in tasks:
            continue
        before_task = tasks[pred]
        link = Link(
            pred,
            before,
            row,
            _ms(before_task.end + timedelta(days=1)),
            start,
            before_task.critical and successor_critical,
        )
        key = (row.number, before.number, 0) if before else (row.number, 0, pred)
        kept = best.get(key)
        if kept is None or (link.critical, link.finish_ms, -link.start_ms) > (
            kept.critical,
            kept.finish_ms,
            -kept.start_ms,
        ):
            best[key] = link
    return list(best.values()), len(wanted)


def _svg_path(points: Iterable[tuple[int, float]]) -> str:
    return " ".join(f"{'M' if i == 0 else 'L'}{x},{y:g}" for i, (x, y) in enumerate(points))


# ---------------------------------------------------------------- hover


def _hover(ctx: ReportContext, row: Row, numbers: dict[IssueId, int], names: Names) -> str:
    """The hover card (§6.9); Jira text is escaped."""
    years = row.start.year != row.end.year
    esc = html.escape
    if row.kind is RowKind.SUMMARY:
        return (
            f"<b>{esc(row.name)}</b>{' · Critical' if row.critical else ''}<br>"
            f"{_short(row.start, years)} → {_short(row.end, years)} · "
            f"{ctx.calendar.working_days_inclusive(row.start, row.end)} working days<br>"
            f"{row.children} tasks · {row.progress:.0%} complete"
        )
    if row.kind is RowKind.MILESTONE and row.milestone:
        m = row.milestone
        lines = [f"<b>{esc(row.name)}</b> · Milestone", f"Forecast {_short(row.start, years)}"]
        if m.target_date:
            slip = m.slip_working_days or 0
            late = f" – {slip:g} working days late" if slip > 0 else " – on time"
            lines.append(f"Target {_short(m.target_date, years)}{late}")
        lines += [_after(ctx, p, numbers, "Reached when") for p in row.predecessors[:3]]
        return "<br>".join(lines)
    t, source = row.task, row.source
    if t is None:
        return ""
    head = f"<b>{t.issue_key}</b> · {esc(row.name)}" if row.name else f"<b>{t.issue_key}</b>"
    progress, method = _progress(source)
    status = esc(str(source.status)) if source else "Unknown"
    lines = [
        head + (" · <b>Critical</b>" if t.critical else ""),
        f"Resource: {esc(names.person(t.lane))}"
        + (" (tentative, chosen by the planner)" if t.tentative else ""),
        f"{_short(t.start, years)} → {_short(t.end, years)} · "
        f"{t.duration_working_days:g} working days ({_effort(t, source)})",
        _reason(ctx, t, numbers, names),
        *_external_lines(source),
        f"Slack: {t.total_float_working_days:g} days · Status: {status} · "
        f"{progress:.0%} complete ({method})",
    ]
    if source and source.due_date and t.end > source.due_date:
        behind = ctx.calendar.working_days_between(source.due_date, t.end)
        lines.append(f"Due {_short(source.due_date, years)} – {behind:g} working days late")
    return "<br>".join(lines)


_CATEGORY_NAMES = {StatusCategory.TO_DO: "To Do", StatusCategory.IN_PROGRESS: "In Progress"}


def _external_lines(source: Task | None) -> list[str]:
    """Open blockers outside the board: the schedule cannot wait for them (warning W002)."""
    if source is None:
        return []
    return [
        f"Blocked by {b.issue_key} (outside the board, {_CATEGORY_NAMES[b.status_category]}); "
        "not scheduled, so this start may be too early"
        for b in source.external_blockers
        if b.status_category is not StatusCategory.DONE
    ]


def _effort(t: ScheduledTask, source: Task | None) -> str:
    if t.effort_source is EffortSource.STORY_POINTS and source and source.story_points is not None:
        return f"{source.story_points:g} SP → {t.duration_working_days:g}d"
    if t.effort_source is EffortSource.ESTIMATE:
        return f"{t.effort_hours:g} h estimate"
    if t.effort_source is EffortSource.REMAINING:
        return f"{t.effort_hours:g} h remaining"
    return f"no estimate; default {t.effort_hours:g} h"


def _after(ctx: ReportContext, issue_id: IssueId, numbers: dict[IssueId, int], verb: str) -> str:
    key = next((t.issue_key for t in ctx.plan.scheduled_tasks if t.issue_id == issue_id), "")
    where = f" (row {numbers[issue_id]})" if issue_id in numbers else " (hidden)"
    return f"{verb} {key}{where} finishes"


def _reason(ctx: ReportContext, t: ScheduledTask, numbers: dict[IssueId, int], names: Names) -> str:
    if t.constraint_reason in REASONS:
        return REASONS[t.constraint_reason]
    if t.constraining_issue_id is None:
        return "Waits for earlier work"
    if t.constraint_reason is ConstraintReason.RESOURCE:
        return _after(ctx, t.constraining_issue_id, numbers, f"{names.person(t.lane)} is free once")
    return _after(ctx, t.constraining_issue_id, numbers, "Starts after")


# ---------------------------------------------------------------- figure


@dataclass
class Canvas:
    """Pixel geometry of the figure."""

    rows: int
    header_px: int
    strip_px: int

    @property
    def paper_px(self) -> int:
        return self.strip_px + max(self.rows, 1) * ROW_PX

    @property
    def height(self) -> int:
        return self.header_px + self.paper_px + LEGEND_PX

    @property
    def top(self) -> float:
        """Top of the row area in paper coordinates."""
        return max(self.rows, 1) * ROW_PX / self.paper_px

    def above(self, px: float) -> float:
        """Paper y of a point ``px`` pixels above the row area."""
        return self.top + px / self.paper_px


def _spans_years(rows: Sequence[Row]) -> bool:
    return len({d.year for r in rows for d in (r.start, r.end)}) > 1


def _pred_tokens(
    ctx: ReportContext, row: Row, numbers: dict[IssueId, int], keys: Mapping[IssueId, str]
) -> list[str]:
    """A row's predecessors: row numbers, keys of hidden rows, open external blockers."""
    preds = [str(numbers[i]) if i in numbers else keys.get(i, "") for i in row.predecessors]
    if row.source:
        preds += [
            f"{b.issue_key} (ext)"
            for b in row.source.external_blockers
            if b.status_category is not StatusCategory.DONE
        ]
    return preds


def _pred_px(ctx: ReportContext, rows: Sequence[Row], numbers: dict[IssueId, int]) -> int:
    """Width of the Pred. column: its default, widened to the longest cell up to the cap."""
    keys = {t.issue_id: t.issue_key for t in ctx.plan.scheduled_tasks}
    longest = max((len(",".join(_pred_tokens(ctx, r, numbers, keys))) for r in rows), default=0)
    default = dict(TABLE_COLUMNS)["Pred."]
    return min(max(default, math.ceil(longest * CHAR_PX) + 8), PRED_MAX_PX)


def _columns(years: bool, pred_px: int | None = None) -> list[tuple[str, float, float]]:
    """Each column's header with its left and right edge in table coordinates (0–1)."""
    table_px = REFERENCE_WIDTH * TABLE_SHARE
    widths = [
        (n, w + (YEAR_PX if years and n in {"Start", "Finish"} else 0)) for n, w in TABLE_COLUMNS
    ]
    if pred_px is not None:
        widths = [(n, pred_px if n == "Pred." else w) for n, w in widths]
    fixed = sum(w for _, w in widths)
    edges, x = [], 0.0
    for name, width in widths:
        w = (width or table_px - fixed) / table_px
        edges.append((name, x, x + w))
        x += w
    return edges


def _cells(
    ctx: ReportContext,
    rows: Sequence[Row],
    numbers: dict[IssueId, int],
    p: Palette,
    pred_px: int | None = None,
) -> list[list[str]]:
    years = _spans_years(rows)
    widths = {
        name: (right - left) * REFERENCE_WIDTH * TABLE_SHARE
        for name, left, right in _columns(years, pred_px)
    }
    chars = {name: int((w - 8) / CHAR_PX) for name, w in widths.items()}
    keys = {t.issue_id: t.issue_key for t in ctx.plan.scheduled_tasks}
    out = []
    for r in rows:
        if r.kind is RowKind.SUMMARY:
            name = f"<b>▾ {html.escape(_fit(r.name, chars['Task name'] - 2))}</b>"
            duration = _days(ctx.calendar.working_days_inclusive(r.start, r.end))
        elif r.kind is RowKind.MILESTONE:
            name = f"  ◆ {html.escape(_fit(r.name, chars['Task name'] - 4))}"
            duration = "0d"
        else:
            key = r.task.issue_key if r.task else ""
            url = (
                html.escape(r.source.url)
                if r.source and r.source.url.startswith("https://")
                else ""
            )
            muted = f'<span style="color:{p.text_muted}">{key}</span>'
            link = (
                f'<a href="{url}" target="_blank" style="color:{p.text_muted}">{key}</a>'
                if url
                else muted
            )
            budget = chars["Task name"] - len(key) - 3
            name = f"  {link} {html.escape(_fit(r.name, budget))}" if r.name else f"  {link}"
            duration = _days(r.task.duration_working_days if r.task else 0)
        preds = _pred_tokens(ctx, r, numbers, keys)
        resource = html.escape(_fit(r.resource, chars["Resource"]))
        if r.resource == UNASSIGNED:
            resource = f'<span style="color:{p.text_muted}">{resource}</span>'
        out.append(
            [
                str(r.number),
                name,
                duration,
                _short(r.start, years),
                _short(r.end, years),
                html.escape(_fit_tokens(preds, chars["Pred."])),
                resource if r.kind is RowKind.TASK else "",
            ]
        )
    return out


def gantt_figure(
    ctx: ReportContext,
    tasks: Sequence[ScheduledTask],
    title: str,
    group_by: GroupBy | None = None,
) -> go.Figure:
    """The split-view Gantt of ``tasks`` (Addendum 02)."""
    if group_by is not None:
        ctx = replace(ctx, chart=replace(ctx.chart, group_by=group_by))
    options, p, names = ctx.chart, PALETTES[ctx.chart.theme], _names(ctx)
    rows, collapsed = build_rows(ctx, tasks)
    links, dependencies = link_list(ctx, rows)
    numbers = {r.task.issue_id: r.number for r in rows if r.task}
    banners = _banners(ctx, tasks, rows, (len(links), dependencies) if collapsed else None)
    sprints = _sprints(ctx)
    canvas = Canvas(
        len(rows),
        30 + 20 + 24 + 24 * len(banners),
        MARK_PX + 2 * TIER_PX + (SPRINT_PX if sprints else 0),
    )
    figure = make_subplots(
        rows=1,
        cols=2,
        shared_yaxes=True,
        horizontal_spacing=0,
        column_widths=[TABLE_SHARE, 1 - TABLE_SHARE],
    )
    _header(figure, ctx, title, banners, canvas, p)
    if not rows:
        message = "No tasks match this filter" if options.filtered else "No open tasks to schedule"
        hint = "Clear the filters to see the whole plan." if options.filtered else ""
        figure.add_annotation(
            text=f"<b>{message}</b><br>{hint}",
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            showarrow=False,
        )
        return _layout(
            figure,
            ctx,
            canvas,
            (ctx.plan.plan_date, ctx.plan.plan_date + timedelta(days=14)),
            p,
            sprints,
        )

    first = min(min(r.start for r in rows), ctx.plan.plan_date)
    last = max(r.end for r in rows)
    if _forecast_markers(ctx):
        last = max(last, ctx.plan.forecast.p85 + timedelta(days=1))
    if any(link.before is None for link in links):
        first -= timedelta(days=7)  # room for the "← ABC-1 (hidden)" stubs
    span = max((last - first).days, 7)
    window = (
        first - timedelta(days=2),
        _monday(last + timedelta(days=max(4, span * 15 // 100))) + timedelta(days=7),
    )
    _table(figure, ctx, rows, numbers, canvas, p)
    _nonworking(figure, ctx, window, len(rows), p)
    _links(figure, ctx, links, (window[1] - window[0]).days, p)
    _bars(figure, ctx, rows, numbers, names, p)
    _markers(figure, ctx, rows, canvas, window, p)
    return _layout(figure, ctx, canvas, window, p, sprints)


def epic_filter_banner(ctx: ReportContext, tasks: Sequence[ScheduledTask]) -> str | None:
    """What an epic filter shows and leaves out; ``None`` without one. Also the first line of the
    ``pb_render`` summary, so the tool result says what the chart says."""
    if not ctx.chart.epics:
        return None
    names = _names(ctx)
    shown = visible_tasks(ctx, tasks)
    ids = {t.issue_id for t in shown}
    hidden = {
        e.predecessor
        for e in ctx.plan.dependency_edges
        if e.successor in ids and e.predecessor not in ids
    }
    external = {
        b.issue_key
        for t in shown
        if (source := ctx.tasks.get(t.issue_id))
        for b in source.external_blockers
        if b.status_category is not StatusCategory.DONE
    }
    label = ", ".join(names.epic(e) for e in ctx.chart.epics)
    return (
        f"Filtered to {label}: {len(shown)} of {len(tasks)} open tasks; "
        f"{len(hidden)} {'blocker' if len(hidden) == 1 else 'blockers'} outside the filter "
        f"(hidden stubs); {len(external)} open "
        f"{'blocker' if len(external) == 1 else 'blockers'} outside the board"
    )


def _banners(
    ctx: ReportContext,
    tasks: Sequence[ScheduledTask],
    rows: Sequence[Row],
    collapsed: tuple[int, int] | None,
) -> list[str]:
    """``collapsed``: link lines drawn and the dependencies they stand for, if collapsed."""
    plan, team = ctx.plan, ctx.settings.team
    out = []
    if (filtered := epic_filter_banner(ctx, tasks)) is not None:
        out.append(filtered)
    if plan.forecast.method is ForecastMethod.MONTE_CARLO:
        out.append(
            f"Sequence view: {plan.estimated_share:.0%} of open work estimated "
            f"(under {team.estimated_threshold:.0%}) – use the forecast range"
        )
    if plan.stale_since:
        since = plan.stale_since.astimezone(ZoneInfo(ctx.time_zone))
        out.append(f"Jira unreachable since {since:%H:%M}; showing the last stored data")
    people = {t.lane for t in tasks if t.lane}
    shown = {r.task.lane for r in rows if r.task and r.task.lane}
    if ctx.chart.assignees and len(people) > MANY_PEOPLE:
        out.append(f"Showing {len(shown)} of {len(people)} people")
    elif len(people) > MANY_PEOPLE:
        out.append(f"{len(people)} people on this plan – filter by assignee to focus")
    if collapsed is not None:
        why = f" (over {COLLAPSE_ABOVE} tasks)" if ctx.chart.collapsed is None else ""
        lines, dependencies = collapsed
        out.append(
            f"{len(visible_tasks(ctx, tasks))} tasks: groups are collapsed{why}; {lines} link "
            f"{'line' if lines == 1 else 'lines'} between groups for {dependencies} "
            "dependencies – render with expand=true to see every task"
        )
    return out


def _header(
    figure: go.Figure,
    ctx: ReportContext,
    title: str,
    banners: Sequence[str],
    canvas: Canvas,
    p: Palette,
) -> None:
    """Title, subtitle, key facts and banners (§6.7), in the top margin."""
    plan, forecast = ctx.plan, ctx.plan.forecast
    board = " + ".join(str(s.board_name) for s in ctx.snapshots) or "Board"
    site = next((s for s in ctx.settings.sites if s.site_id == plan.site_id), None)
    generated = plan.generated_at.astimezone(ZoneInfo(ctx.time_zone))
    subtitle = " · ".join(
        (
            DEPLOYMENTS.get(site.deployment, "Jira") if site else "Jira",
            f"plan date {plan.plan_date:%b} {plan.plan_date.day}, {plan.plan_date.year}",
            f"team time zone {ctx.time_zone}",
            f"generated {generated:%H:%M}",
        )
    )
    critical = sum(1 for t in plan.scheduled_tasks if t.critical)
    years = forecast.p85.year != plan.plan_date.year
    facts = "   ·   ".join(
        (
            f"Forecast end <b>{_short(forecast.p50, years)}</b> "
            f"(P85 {_short(forecast.p85, years)})",
            f"Confidence: <b>{forecast.confidence.value.capitalize()}</b> – "
            f"{html.escape(_fit(forecast.confidence_reason, 70))}",
            f"Critical tasks: <b>{critical}</b>",
            f"Warnings: <b>{len(plan.warnings)}</b>",
        )
    )
    lines = [
        (f"<b>{html.escape(board)} – {html.escape(title)}</b>", 18, p.text, 30),
        (html.escape(subtitle), 12, p.text_secondary, 20),
        (facts, 12, p.text, 24),
    ]
    lines += [(f"⚠ {html.escape(b)}", 12, p.text, 24) for b in banners]
    y = canvas.header_px
    for i, (text, size, color, height) in enumerate(lines):
        banner = i >= 3
        figure.add_annotation(
            text=text,
            x=0,
            xref="paper",
            xanchor="left",
            y=1 + y / canvas.paper_px,
            yref="paper",
            yanchor="top",
            showarrow=False,
            font={"size": size, "color": color},
            bgcolor=p.header if banner else None,
            bordercolor=p.late if banner else None,
            borderpad=3 if banner else 0,
            borderwidth=1 if banner else 0,
        )
        y -= height


def _table(
    figure: go.Figure,
    ctx: ReportContext,
    rows: Sequence[Row],
    numbers: dict[IssueId, int],
    canvas: Canvas,
    p: Palette,
) -> None:
    """The task table (§6.1): one text trace per column, header labels in the strip."""
    pred_px = _pred_px(ctx, rows, numbers)
    cells = _cells(ctx, rows, numbers, p, pred_px)
    ys = [r.number - 1 for r in rows]
    names = _names(ctx)
    hovers = [_hover(ctx, r, numbers, names) for r in rows]
    for index, (header, left, right) in enumerate(_columns(_spans_years(rows), pred_px)):
        right_aligned = header in {"ID", "Dur."}
        x = right - 0.008 if right_aligned else left + 0.008
        figure.add_trace(
            go.Scatter(
                x=[x] * len(rows),
                y=ys,
                mode="text",
                text=[c[index] for c in cells],
                textposition="middle left" if right_aligned else "middle right",
                textfont={"size": 12, "color": p.text_secondary if header == "ID" else p.text},
                hovertext=hovers if header == "Task name" else None,
                hovertemplate="%{hovertext}<extra></extra>" if header == "Task name" else None,
                hoverinfo=None if header == "Task name" else "skip",
                showlegend=False,
                name=header,
            ),
            row=1,
            col=1,
        )
        figure.add_annotation(
            text=f"<b>{header}</b>",
            x=x,
            xref="x",
            xanchor="right" if right_aligned else "left",
            y=canvas.above(MARK_PX + TIER_PX / 2),
            yref="paper",
            yanchor="middle",
            showarrow=False,
            font={"size": 12, "color": p.text_secondary},
        )
    critical = [r.number - 1 for r in rows if r.critical and r.kind is not RowKind.SUMMARY]
    figure.add_trace(
        go.Scatter(
            x=[0.012] * len(critical),
            y=critical,
            mode="markers",
            marker={"symbol": "square", "size": 7, "color": p.critical},
            hovertemplate="Critical<extra></extra>",
            showlegend=False,
            name="Critical marker",
        ),
        row=1,
        col=1,
    )
    for r in rows:
        if r.kind is RowKind.SUMMARY:
            figure.add_shape(
                type="rect",
                xref="paper",
                x0=0,
                x1=1,
                yref="y",
                y0=r.number - 1.5,
                y1=r.number - 0.5,
                fillcolor=p.header,
                line_width=0,
                layer="below",
            )


def _nonworking(
    figure: go.Figure, ctx: ReportContext, window: tuple[date, date], count: int, p: Palette
) -> None:
    """Weekends and team holidays, shaded full height; holiday names on hover (§6.2)."""
    day = window[0]
    runs: list[list[date]] = []
    holidays: list[tuple[date, str]] = []
    while day < window[1]:
        if not ctx.calendar.is_working_day(day):
            if runs and runs[-1][1] == day:
                runs[-1][1] = day + timedelta(days=1)
            else:
                runs.append([day, day + timedelta(days=1)])
            if name := ctx.calendar.holiday_name(day):
                holidays.append((day, name))
        day += timedelta(days=1)
    for i, (a, b) in enumerate(runs):
        figure.add_shape(
            type="rect",
            xref="x2",
            x0=_iso(a),
            x1=_iso(b),
            yref="y2 domain",
            y0=0,
            y1=1,
            fillcolor=p.nonworking,
            line_width=0,
            layer="below",
            name="Nonworking time",
            legendgroup="nonworking",
            showlegend=i == 0,
            legendrank=8,
        )
    if holidays:
        figure.add_trace(
            go.Scatter(
                x=[f"{_iso(d)} 12:00" for d, _ in holidays for _ in range(count)],
                y=[y for _ in holidays for y in range(count)],
                mode="markers",
                marker={"size": 18, "opacity": 0, "color": p.nonworking},
                hovertext=[
                    f"{html.escape(n)} (holiday)" for _, n in holidays for _ in range(count)
                ],
                hovertemplate="%{hovertext}<extra></extra>",
                showlegend=False,
                name="Holidays",
            ),
            row=1,
            col=2,
        )


def _links(
    figure: go.Figure,
    ctx: ReportContext,
    links: Sequence[Link],
    span_days: int,
    p: Palette,
) -> None:
    """Right-angle link lines with arrowheads, one toggleable legend layer (§6.5)."""
    stub = max(DAY_MS * 35 // 100, span_days * DAY_MS * 6 // 1000)
    keys = {t.issue_id: t.issue_key for t in ctx.plan.scheduled_tasks}
    first = True
    for link in links:
        line = {
            "color": p.critical if link.critical else p.link,
            "width": 1.5 if link.critical else 1,
        }
        group = {"legendgroup": "links", "name": "Dependency", "legendrank": 6}
        start, y = link.start_ms, link.row.number - 1
        if link.before is None:
            figure.add_shape(
                type="line",
                xref="x2",
                yref="y2",
                x0=start - 3 * stub,
                x1=start,
                y0=y,
                y1=y,
                line=line,
                layer="between",
                showlegend=False,
                label={
                    "text": f"← {keys.get(link.predecessor, '')} (hidden)",
                    "textposition": "start",
                    "xanchor": "right",
                    "font": {"size": 11, "color": p.text_secondary},
                },
                **group,
            )
            continue
        path = link_path(link.finish_ms, link.before.number - 1, start, y, stub)
        figure.add_shape(
            type="path",
            fillcolor=OPEN,
            xref="x2",
            yref="y2",
            path=_svg_path(path),
            line=line,
            layer="between",
            showlegend=first,
            **group,
        )
        figure.add_shape(
            type="path",
            xref="x2",
            yref="y2",
            xsizemode="pixel",
            ysizemode="pixel",
            xanchor=start,
            yanchor=y,
            path="M-6,-3.5 L0,0 L-6,3.5 Z",
            fillcolor=line["color"],
            line={"width": 0},
            layer="between",
            showlegend=False,
            **group,
        )
        first = False


def _bars(
    figure: go.Figure,
    ctx: ReportContext,
    rows: Sequence[Row],
    numbers: dict[IssueId, int],
    names: Names,
    p: Palette,
) -> None:
    """Task, progress and summary bars, resource labels and milestones (§6.3, §6.4)."""
    options = ctx.chart
    tasks = [r for r in rows if r.kind is RowKind.TASK]
    epic_colors = _epic_colors(ctx, p)
    groups: dict[str, list[Row]] = {}
    for r in tasks:
        if options.color_by is ColorBy.EPIC:
            epic = r.source.epic_issue_id if r.source else None
            label = names.epics.get(epic, NO_EPIC) if epic is not None else NO_EPIC
            groups.setdefault(label, []).append(r)
        else:
            groups.setdefault("Critical task" if r.critical else "Task", []).append(r)
    for rank, (label, members) in enumerate(
        sorted(groups.items(), key=lambda kv: (kv[0] == "Critical task", kv[0]))
    ):
        first = members[0]
        epic = first.source.epic_issue_id if first.source else None
        color = (
            epic_colors.get(epic, p.done)
            if options.color_by is ColorBy.EPIC
            else p.critical
            if label == "Critical task"
            else p.task
        )
        edges = [
            p.critical
            if r.critical
            else p.critical_progress
            if label == "Critical task"
            else p.progress
            for r in members
        ]
        figure.add_trace(
            go.Bar(
                orientation="h",
                y=[r.number - 1 for r in members],
                base=[_iso(r.start) for r in members],
                x=[(r.end - r.start).days * DAY_MS + DAY_MS for r in members],
                width=BAR,
                marker={
                    "color": color,
                    "opacity": [0.4 if r.task and r.task.tentative else 1.0 for r in members],
                    "line": {
                        "color": edges,
                        "width": [
                            2 if r.critical and options.color_by is ColorBy.EPIC else 1
                            for r in members
                        ],
                    },
                    "pattern": {"shape": "/", "fgcolor": p.surface, "fgopacity": 0.7, "size": 6}
                    if options.patterns and label == "Critical task"
                    else None,
                },
                customdata=[r.source.url if r.source else "" for r in members],
                hovertext=[_hover(ctx, r, numbers, names) for r in members],
                hovertemplate="%{hovertext}<extra></extra>",
                name=label,
                legendrank=1 + rank,
            ),
            row=1,
            col=2,
        )
    for r in tasks:
        if r.task and r.task.tentative:
            figure.add_shape(
                type="rect",
                xref="x2",
                yref="y2",
                x0=_iso(r.start),
                x1=_iso(r.end + timedelta(days=1)),
                y0=r.number - 1 - BAR / 2,
                y1=r.number - 1 + BAR / 2,
                line={"color": p.critical if r.critical else p.task, "width": 1, "dash": "dash"},
                layer="above",
            )
    for critical in (False, True):
        done = [r for r in tasks if r.progress > 0 and r.critical == critical]
        if done:
            figure.add_trace(
                go.Bar(
                    orientation="h",
                    y=[r.number - 1 for r in done],
                    base=[_iso(r.start) for r in done],
                    x=[round(((r.end - r.start).days + 1) * DAY_MS * r.progress) for r in done],
                    width=PROGRESS,
                    marker={
                        "color": p.critical_progress if critical else p.progress,
                        "line": {"width": 0},
                    },
                    hoverinfo="skip",
                    name="Progress",
                    legendgroup="progress",
                    showlegend=not critical
                    or not any(r.progress > 0 and not r.critical for r in tasks),
                    legendrank=3,
                ),
                row=1,
                col=2,
            )
    summaries = [r for r in rows if r.kind is RowKind.SUMMARY]
    if summaries:
        figure.add_trace(
            go.Bar(
                orientation="h",
                y=[r.number - 1 - 0.05 for r in summaries],
                base=[_iso(r.start) for r in summaries],
                x=[(r.end - r.start).days * DAY_MS + DAY_MS for r in summaries],
                width=SUMMARY,
                marker={
                    "color": p.summary,
                    "line": {
                        "color": [p.critical if r.critical else p.summary for r in summaries],
                        "width": 1.5,
                    },
                },
                hovertext=[_hover(ctx, r, numbers, names) for r in summaries],
                hovertemplate="%{hovertext}<extra></extra>",
                name="Summary",
                legendgroup="summary",
                legendrank=4,
            ),
            row=1,
            col=2,
        )
        xs, ys = [], []
        for r in summaries:
            a, b, y = _ms(r.start), _ms(r.end + timedelta(days=1)), r.number - 1
            w = min(DAY_MS * 4 // 10, (b - a) // 4)
            for x0, x1 in ((a, a + w), (b, b - w)):
                xs += [x0, x1, x0, x0, None]
                ys += [y + 0.05, y + 0.05, y + 0.3, y + 0.05, None]
        figure.add_trace(
            go.Scatter(
                x=[_stamp(x) if x is not None else None for x in xs],
                y=ys,
                mode="lines",
                fill="toself",
                fillcolor=p.summary,
                line={"width": 0},
                hoverinfo="skip",
                legendgroup="summary",
                showlegend=False,
                name="Summary ends",
            ),
            row=1,
            col=2,
        )
    labels = [r for r in tasks if r.resource]
    figure.add_trace(
        go.Scatter(
            x=[_iso(r.end + timedelta(days=1)) for r in labels],
            y=[r.number - 1 for r in labels],
            mode="text",
            text=[f" {html.escape(r.resource)}" for r in labels],
            textposition="middle right",
            textfont={"size": 11, "color": p.text_secondary},
            hoverinfo="skip",
            showlegend=False,
            name="Resource labels",
        ),
        row=1,
        col=2,
    )
    milestones = [r for r in rows if r.kind is RowKind.MILESTONE]
    if milestones:
        figure.add_trace(
            go.Scatter(
                x=[_iso(r.start + timedelta(days=1)) for r in milestones],
                y=[r.number - 1 for r in milestones],
                mode="markers+text",
                marker={
                    "symbol": "diamond",
                    "size": 13,
                    "color": p.milestone,
                    "line": {"width": 1, "color": p.surface},
                },
                text=[f"  {_milestone_label(r)}" for r in milestones],
                textposition="middle right",
                textfont={"size": 11, "color": p.text_secondary},
                hovertext=[_hover(ctx, r, numbers, names) for r in milestones],
                hovertemplate="%{hovertext}<extra></extra>",
                name="Milestone",
                legendrank=5,
            ),
            row=1,
            col=2,
        )


def _milestone_label(row: Row) -> str:
    label = f"{row.start:%b} {row.start.day}"
    m = row.milestone
    if m and m.target_date and (m.slip_working_days or 0) > 0:
        label += f"  ⚑ +{m.slip_working_days:g}d vs {m.target_date:%b} {m.target_date.day}"
    return label


def _epic_colors(ctx: ReportContext, p: Palette) -> dict[IssueId | None, str]:
    """Categorical slots in fixed order of epic key, so filters never repaint an epic."""
    epics = sorted(
        {e.issue_id: e.issue_key for s in ctx.snapshots for e in s.epics}.items(),
        key=lambda kv: kv[1],
    )
    return {
        issue_id: p.categorical[i] for i, (issue_id, _) in enumerate(epics[: len(p.categorical)])
    }


def _markers(
    figure: go.Figure,
    ctx: ReportContext,
    rows: Sequence[Row],
    canvas: Canvas,
    window: tuple[date, date],
    p: Palette,
) -> None:
    """Today, forecast markers and late flags (§6.2, §5)."""
    plan = ctx.plan
    lines = [(plan.plan_date, "Today", p.critical, "dash", 1.5)]
    if _forecast_markers(ctx):
        f = plan.forecast
        lines += [
            (f.p50 + timedelta(days=1), f"P50 {f.p50:%b} {f.p50.day}", p.text_secondary, "dash", 1),
            (f.p85 + timedelta(days=1), f"P85 {f.p85:%b} {f.p85.day}", p.text_secondary, "dot", 1),
        ]
    for day, label, color, dash, width in lines:
        if not window[0] <= day <= window[1]:
            continue
        today = label == "Today"
        figure.add_shape(
            type="line",
            xref="x2",
            x0=_iso(day),
            x1=_iso(day),
            yref="y2 domain",
            y0=0,
            y1=1,
            line={"color": color, "width": width, "dash": dash},
            layer="above",
            name=label,
            showlegend=today,
            legendrank=7 if today else 9,
        )
        figure.add_annotation(
            text=label,
            x=_iso(day),
            xref="x2",
            xanchor="left",
            y=canvas.top,
            yref="paper",
            yanchor="bottom",
            showarrow=False,
            font={"size": 10, "color": p.text_secondary},
        )
    late = [
        r
        for r in rows
        if r.source and r.source.due_date and r.task and r.task.end > r.source.due_date
    ]
    if late:
        figure.add_trace(
            go.Scatter(
                x=[
                    _iso(r.source.due_date + timedelta(days=1))
                    for r in late
                    if r.source and r.source.due_date
                ],
                y=[r.number - 1.38 for r in late],
                mode="markers",
                marker={"symbol": "triangle-down", "size": 9, "color": p.late},
                hovertext=[
                    f"Due {r.source.due_date:%b} {r.source.due_date.day}"
                    for r in late
                    if r.source and r.source.due_date
                ],
                hovertemplate="⚑ %{hovertext}<extra></extra>",
                name="Late (due date)",
                legendrank=10,
            ),
            row=1,
            col=2,
        )


def _forecast_markers(ctx: ReportContext) -> bool:
    """P50/P85 lines for a low-confidence sequence view or a Kanban board (§6.2)."""
    kanban = any(s.board_type is BoardType.KANBAN for s in ctx.snapshots)
    return ctx.plan.forecast.method is ForecastMethod.MONTE_CARLO or kanban


def _sprints(ctx: ReportContext) -> list[tuple[date, date, str]]:
    if not any(s.board_type is BoardType.SCRUM for s in ctx.snapshots):
        return []
    out = []
    for s in ctx.snapshots:
        for sprint in s.sprints:
            if (
                sprint.start
                and sprint.end
                and sprint.state in {SprintState.ACTIVE, SprintState.FUTURE}
            ):
                start, end = (
                    local_date(sprint.start, ctx.time_zone),
                    local_date(sprint.end, ctx.time_zone),
                )
                out.append((start, end + timedelta(days=1), str(sprint.name)))
    return sorted(out)


def _ranges(
    ctx: ReportContext, window: tuple[date, date], sprints: Sequence[tuple[date, date, str]]
) -> list[tuple[str, date, date]]:
    """Range buttons (§6.10): each sets the visible range and its timescale tiers."""
    start = _monday(ctx.plan.plan_date)
    out = [("2 weeks", start, start + timedelta(days=14))]
    active = [(a, b) for a, b, _ in sprints if a <= ctx.plan.plan_date < b]
    if active:
        out.append(("Sprint", *active[0]))
    out += [
        ("Month", start, start + timedelta(days=35)),
        ("Quarter", start, start + timedelta(days=91)),
    ]
    out.append(("All", *window))
    return out


def _layout(
    figure: go.Figure,
    ctx: ReportContext,
    canvas: Canvas,
    window: tuple[date, date],
    p: Palette,
    sprints: Sequence[tuple[date, date, str]],
) -> go.Figure:
    """Axes, the header strip, the two-tier timescale, range buttons and the legend."""
    rows = max(canvas.rows, 1)
    tiers = tiers_for(*window)
    hidden = {"visible": False, "fixedrange": True}
    tier_axis = {
        "overlaying": "x2",
        "matches": "x2",
        "type": "date",
        "showline": False,
        "ticks": "",
    }
    row_bounds = [i + 0.5 for i in range(rows - 1)]
    figure.update_layout(
        template=template_name(ctx.chart.theme),
        height=canvas.height,
        autosize=True,
        margin={"l": 8, "r": 8, "t": canvas.header_px, "b": LEGEND_PX},
        barmode="overlay",
        bargap=0,
        dragmode="pan",
        hovermode="closest",
        showlegend=True,
        legend={
            "orientation": "h",
            "x": 0,
            "xanchor": "left",
            "y": -6 / canvas.paper_px,
            "yanchor": "top",
            "traceorder": "normal",
            "font": {"size": 12},
        },
        xaxis={**hidden, "range": [0, 1], "domain": [0, TABLE_SHARE]},
        xaxis2={
            "type": "date",
            "domain": [TABLE_SHARE, 1],
            "range": [_iso(window[0]), _iso(window[1])],
            "side": "top",
            "anchor": "y2",
            "ticklabelstandoff": MARK_PX,
            "tickmode": "array",
            "ticks": "",
            "showgrid": False,
            "showline": False,
            "tickfont": {"size": 11, "color": p.text_secondary},
        },
        xaxis3={
            **tier_axis,
            "side": "top",
            "anchor": "y2",
            "ticklabelstandoff": MARK_PX + TIER_PX,
            "tickmode": "array",
            "showgrid": False,
            "tickfont": {"size": 11, "color": p.text},
        },
        xaxis4={
            **tier_axis,
            "side": "bottom",
            "anchor": "y2",
            "showticklabels": False,
            "showgrid": True,
            "gridcolor": p.grid,
            "tickmode": "array",
        },
        xaxis5={
            **tier_axis,
            "side": "bottom",
            "anchor": "y2",
            "showticklabels": False,
            "showgrid": True,
            "gridcolor": p.text_muted,
            "tickmode": "array",
        },
        yaxis={
            "range": [rows - 0.5, -0.5],
            "domain": [0, canvas.top],
            "fixedrange": True,
            "tickmode": "array",
            "tickvals": row_bounds,
            "showticklabels": False,
            "showgrid": True,
            "gridcolor": p.grid,
            "ticks": "",
            "showline": False,
            "zeroline": False,
        },
        yaxis2={
            "matches": "y",
            "domain": [0, canvas.top],
            "fixedrange": True,
            "tickmode": "array",
            "tickvals": row_bounds,
            "showticklabels": False,
            "showgrid": True,
            "gridcolor": p.grid,
            "ticks": "",
            "showline": False,
            "zeroline": False,
        },
        updatemenus=[
            {
                "type": "buttons",
                "direction": "right",
                "showactive": ctx.chart.theme is ThemeMode.LIGHT,  # Plotly's highlight is light
                "active": len(_ranges(ctx, window, sprints)) - 1,
                "x": 1,
                "xanchor": "right",
                "y": 1 + (canvas.header_px - 50) / canvas.paper_px,
                "yanchor": "top",
                "bgcolor": p.surface,
                "bordercolor": p.grid,
                "font": {"size": 11, "color": p.text_secondary},
                "pad": {"r": 0, "t": 0},
                "buttons": [
                    {
                        "label": label,
                        "method": "relayout",
                        "args": [
                            {"xaxis2.range": [_iso(a), _iso(b)], **_tier_axes(tiers_for(a, b))}
                        ],
                    }
                    for label, a, b in _ranges(ctx, window, sprints)
                ],
            }
        ],
        modebar={"bgcolor": "rgba(0,0,0,0)", "color": p.text_muted, "activecolor": p.text},
    )
    figure.update_layout(_tier_axes(tiers))
    for axis in ("x3", "x4", "x5"):  # Plotly draws an overlaying axis only when a trace uses it
        figure.add_trace(
            go.Scatter(
                x=[_iso(window[0])],
                y=[0],
                mode="none",
                xaxis=axis,
                yaxis="y2",
                hoverinfo="skip",
                showlegend=False,
                name="Timescale",
            )
        )
    figure.add_shape(
        type="rect",
        xref="paper",
        x0=0,
        x1=1,
        yref="paper",
        y0=canvas.top,
        y1=1,
        fillcolor=p.header,
        line_width=0,
        layer="below",
    )
    figure.add_shape(
        type="line",
        xref="paper",
        x0=0,
        x1=1,
        yref="paper",
        y0=canvas.top,
        y1=canvas.top,
        line={"color": p.grid, "width": 1},
    )
    figure.add_shape(
        type="line",
        xref="paper",
        x0=TABLE_SHARE,
        x1=TABLE_SHARE,
        yref="paper",
        y0=0,
        y1=1,
        line={"color": p.text_muted, "width": 1},
    )
    for a, b, name in sprints:
        if b <= window[0] or a >= window[1]:
            continue
        y0, y1 = canvas.above(MARK_PX + 2 * TIER_PX + 3), canvas.above(MARK_PX + 2 * TIER_PX + 7)
        figure.add_shape(
            type="path",
            fillcolor=OPEN,
            xref="x2",
            yref="paper",
            path=f"M{_ms(a)},{y0} L{_ms(a)},{y1} L{_ms(b)},{y1} L{_ms(b)},{y0}",
            line={"color": p.text_muted, "width": 1},
        )
        lo, hi = max(a, window[0]), min(b, window[1])
        mid = lo + (hi - lo) / 2
        figure.add_annotation(
            text=html.escape(name),
            x=_iso(mid),
            xref="x2",
            y=y1,
            yref="paper",
            yanchor="bottom",
            showarrow=False,
            font={"size": 11, "color": p.text_secondary},
        )
    return figure
