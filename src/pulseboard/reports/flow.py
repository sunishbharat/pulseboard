"""Flow reports: burn-up/down, cumulative flow, cycle time, throughput, aging WIP, Monte Carlo."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from pulseboard.domain.snapshot import BoardSnapshot, Sprint, Task
from pulseboard.domain.types import BoardType, IssueId, ReportId, SprintState, StatusCategory
from pulseboard.engine.calendar import local_date
from pulseboard.engine.forecaster import PERCENTILES, seed_for, simulate_trials, weeks_to_date
from pulseboard.reports.base import Report, ReportContext, ReportResult, Table, base_layout

EMPTY = "No data in the window."


def _end_of_day(day: date, time_zone: str) -> datetime:
    return datetime.combine(day + timedelta(days=1), time(0), ZoneInfo(time_zone)).astimezone(UTC)


def _sprint_time(sprint: Sprint) -> datetime:
    return sprint.end or sprint.start or datetime.min.replace(tzinfo=UTC)


def _burn_sprint(snapshot: BoardSnapshot) -> Sprint | None:
    sprints = [s for s in snapshot.sprints if s.start is not None]
    active = [s for s in sprints if s.state is SprintState.ACTIVE]
    closed = sorted((s for s in sprints if s.state is SprintState.CLOSED), key=_sprint_time)
    return active[0] if active else (closed[-1] if closed else None)


def _members_at(snapshot: BoardSnapshot, sprint: Sprint, instant: datetime) -> set[IssueId]:
    """Sprint membership at ``instant``: current members with later changes undone."""
    members = {t.issue_id for t in snapshot.tasks if t.sprint_id == sprint.sprint_id}
    later = sorted(
        (c for c in snapshot.sprint_changes if c.sprint_id == sprint.sprint_id and c.at >= instant),
        key=lambda c: (c.at, c.entry_id),
        reverse=True,
    )
    for change in later:
        if change.added:
            members.discard(change.issue_id)
        else:
            members.add(change.issue_id)
    return members


def burn_rows(ctx: ReportContext) -> tuple[str, tuple[tuple[date, int, int, float, float], ...]]:
    """(scope label, rows of day, scope items, done items, scope points, done points)."""
    if not ctx.snapshots:
        return "", ()
    snapshot, tz, today = ctx.snapshots[0], ctx.time_zone, ctx.plan.plan_date
    tasks = {t.issue_id: t for t in snapshot.tasks}
    sprint = _burn_sprint(snapshot) if snapshot.board_type is BoardType.SCRUM else None
    if sprint is not None and sprint.start is not None:
        first = local_date(sprint.start, tz)
        last = min(local_date(sprint.end, tz), today) if sprint.end else today
        label = str(sprint.name)
    else:
        first, last = today - timedelta(days=ctx.settings.limits.reporting_window_days), today
        label = "window"

    def done_by(task: Task, instant: datetime) -> bool:
        return (
            task.status_category is StatusCategory.DONE
            and task.resolved is not None
            and task.resolved < instant
        )

    rows = []
    for offset in range((last - first).days + 1):
        day = first + timedelta(days=offset)
        instant = _end_of_day(day, tz)
        if sprint is not None:
            scope = [tasks[i] for i in _members_at(snapshot, sprint, instant) if i in tasks]
        else:
            scope = [t for t in tasks.values() if t.created < instant]
        scope = [t for t in scope if t.created < instant]
        done = [t for t in scope if done_by(t, instant)]
        rows.append(
            (
                day,
                len(scope),
                len(done),
                sum(t.story_points or 0.0 for t in scope),
                sum(t.story_points or 0.0 for t in done),
            )
        )
    return label, tuple(rows)


def _burn(ctx: ReportContext) -> ReportResult:
    label, rows = burn_rows(ctx)
    table = Table("burn", ("day", "scope_items", "done_items", "scope_points", "done_points"), rows)
    frame = pd.DataFrame(rows, columns=list(table.columns))
    long = frame.melt(
        id_vars="day",
        value_vars=["scope_items", "done_items"],
        var_name="series",
        value_name="items",
    )
    figure = px.line(
        long,
        x="day",
        y="items",
        color="series",
        markers=True,
        category_orders={"series": ["scope_items", "done_items"]},
    )
    if rows:
        frame["remaining"] = frame["scope_items"] - frame["done_items"]
        figure.add_trace(
            go.Scatter(
                x=frame["day"],
                y=frame["remaining"],
                name="remaining (burn-down)",
                line={"dash": "dash"},
            )
        )
    last = rows[-1] if rows else None
    summary = (
        f"{label}: {last[2]} of {last[1]} items done ({last[4]:g} of {last[3]:g} points)."
        if last
        else EMPTY,
    )
    return ReportResult(
        ReportId.BURN,
        "Burn-up / burn-down",
        (table,),
        base_layout(figure, f"Burn-up / burn-down: {label}", ctx.chart.theme),
        summary,
    )


def _cumulative_flow(ctx: ReportContext) -> ReportResult:
    points = ctx.flow.cfd if ctx.flow else ()
    rows = tuple((p.day, p.column, p.count) for p in points)
    table = Table("cumulative_flow", ("day", "column", "issues"), rows)
    columns = [str(c.name) for c in ctx.snapshots[0].columns] if ctx.snapshots else []
    frame = pd.DataFrame(rows, columns=list(table.columns))
    figure = px.area(
        frame,
        x="day",
        y="issues",
        color="column",
        category_orders={"column": list(reversed(columns))},
    )
    return ReportResult(
        ReportId.CUMULATIVE_FLOW,
        "Cumulative flow",
        (table,),
        base_layout(figure, "Cumulative flow", ctx.chart.theme),
        (f"{len(columns)} columns over {len({r[0] for r in rows})} days." if rows else EMPTY,),
    )


def _cycle_time(ctx: ReportContext) -> ReportResult:
    flow = ctx.flow
    cycles = flow.cycle_times_days if flow else {}
    resolved = {i: ctx.tasks[i].resolved for i in cycles if i in ctx.tasks}
    rows = tuple(
        (ctx.tasks[i].issue_key, local_date(at, ctx.time_zone), days)
        for i, days in cycles.items()
        if (at := resolved.get(i)) is not None
    )
    table = Table("cycle_time", ("key", "done", "cycle_days"), rows)
    frame = pd.DataFrame(rows, columns=list(table.columns))
    figure = px.scatter(frame, x="done", y="cycle_days", hover_name="key")
    percentiles = flow.cycle_time_percentiles if flow else None
    if percentiles:
        for p, value in zip(PERCENTILES, percentiles, strict=True):
            figure.add_hline(y=value, line_dash="dot", annotation_text=f"P{p} {value:.1f} d")
    summary = (
        f"{len(rows)} items; P50 {percentiles[0]:.1f} d, P85 {percentiles[1]:.1f} d."
        if percentiles
        else EMPTY,
    )
    return ReportResult(
        ReportId.CYCLE_TIME,
        "Cycle time",
        (table,),
        base_layout(figure, "Cycle time (days)", ctx.chart.theme),
        summary,
    )


def _throughput(ctx: ReportContext) -> ReportResult:
    weekly = ctx.throughput.weekly
    plan_date = ctx.plan.plan_date
    rows = tuple(
        (plan_date - timedelta(days=7 * (len(weekly) - 1 - i) + 6), count)
        for i, count in enumerate(weekly)
    )
    table = Table("throughput", ("week_start", "done"), rows)
    figure = px.bar(pd.DataFrame(rows, columns=list(table.columns)), x="week_start", y="done")
    mean = sum(weekly) / len(weekly) if weekly else 0.0
    return ReportResult(
        ReportId.THROUGHPUT,
        "Throughput",
        (table,),
        base_layout(figure, "Done items per week", ctx.chart.theme),
        (f"{sum(weekly)} done in {len(weekly)} weeks ({mean:.1f}/week)." if weekly else EMPTY,),
    )


def _aging_wip(ctx: ReportContext) -> ReportResult:
    flow = ctx.flow
    ages = flow.aging_days if flow else {}
    p85 = flow.cycle_time_p85 if flow else None
    rows = tuple(
        (
            ctx.tasks[i].issue_key,
            str(ctx.tasks[i].status),
            age,
            "yes" if p85 is not None and age > p85 else "no",
        )
        for i, age in sorted(ages.items(), key=lambda item: (-item[1], item[0]))
        if i in ctx.tasks
    )
    table = Table("aging_wip", ("key", "status", "age_days", "older_than_p85"), rows)
    figure = px.scatter(
        pd.DataFrame(rows, columns=list(table.columns)),
        x="status",
        y="age_days",
        hover_name="key",
        color="older_than_p85",
        color_discrete_map={"yes": "#d32f2f", "no": "#1e88e5"},
    )
    if p85 is not None:
        figure.add_hline(y=p85, line_dash="dot", annotation_text=f"cycle-time P85 {p85:.1f} d")
    outliers = sum(1 for r in rows if r[3] == "yes")
    return ReportResult(
        ReportId.AGING_WIP,
        "Aging WIP",
        (table,),
        base_layout(figure, "Age of work in progress (days)", ctx.chart.theme),
        (f"{len(rows)} in progress; {outliers} older than the cycle-time P85.",),
    )


def _monte_carlo(ctx: ReportContext) -> ReportResult:
    limits = ctx.settings.limits
    remaining = len(ctx.plan.scheduled_tasks)
    trials = simulate_trials(
        remaining, ctx.throughput.weekly, limits.monte_carlo_trials, seed_for(ctx.plan.plan_id)
    )
    if trials is None:
        empty = Table("monte_carlo", ("percentile", "weeks", "date"), ())
        return ReportResult(
            ReportId.MONTE_CARLO,
            "Monte Carlo forecast",
            (empty,),
            None,
            ("No throughput history to sample.",),
        )
    weeks = np.percentile(trials, PERCENTILES, method="higher")
    rows = tuple(
        (f"P{p}", int(w), weeks_to_date(int(w), ctx.calendar))
        for p, w in zip(PERCENTILES, weeks, strict=True)
    )
    table = Table("monte_carlo", ("percentile", "weeks", "date"), rows)
    counts = np.bincount(trials)
    distribution = Table(
        "distribution",
        ("weeks", "date", "trials"),
        tuple((w, weeks_to_date(w, ctx.calendar), int(n)) for w, n in enumerate(counts) if n),
    )
    frame = pd.DataFrame(distribution.rows, columns=list(distribution.columns))
    figure = px.bar(frame, x="date", y="trials")
    for label, _, day in rows:
        instant = datetime.combine(day, time(12)).isoformat()
        figure.add_shape(
            type="line",
            x0=instant,
            x1=instant,
            y0=0,
            y1=1,
            yref="paper",
            line={"dash": "dot", "color": "#d32f2f"},
        )
        figure.add_annotation(
            x=instant, y=1, yref="paper", text=label, showarrow=False, yanchor="bottom"
        )
    return ReportResult(
        ReportId.MONTE_CARLO,
        "Monte Carlo forecast",
        (table, distribution),
        base_layout(
            figure, f"Completion dates ({limits.monte_carlo_trials} trials)", ctx.chart.theme
        ),
        (f"P50 {rows[0][2]}, P85 {rows[1][2]}, P95 {rows[2][2]} for {remaining} items.",),
    )


burn = Report(ReportId.BURN, "Burn-up / burn-down", "1.0", _burn)
cumulative_flow = Report(ReportId.CUMULATIVE_FLOW, "Cumulative flow", "1.0", _cumulative_flow)
cycle_time = Report(ReportId.CYCLE_TIME, "Cycle time", "1.0", _cycle_time)
throughput = Report(ReportId.THROUGHPUT, "Throughput", "1.0", _throughput)
aging_wip = Report(ReportId.AGING_WIP, "Aging WIP", "1.0", _aging_wip)
monte_carlo = Report(ReportId.MONTE_CARLO, "Monte Carlo forecast", "1.0", _monte_carlo)
