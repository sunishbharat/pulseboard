"""Schedule reports: Gantt / sequence view, critical path, dependency map, milestones (§14.1)."""

import html
from collections.abc import Sequence

import networkx as nx
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from pulseboard.domain.plan import ScheduledTask
from pulseboard.domain.types import ForecastMethod, ReportId, StatusCategory
from pulseboard.reports.base import (
    GroupBy,
    Report,
    ReportContext,
    ReportResult,
    Table,
    base_layout,
)
from pulseboard.reports.gantt import gantt_figure

LOW_CONFIDENCE_TITLE = "Sequence view: low confidence"
GANTT_TITLE = "Delivery plan"

SCHEDULE_COLUMNS = (
    "key",
    "summary",
    "assignee",
    "start",
    "end",
    "effort_h",
    "effort_source",
    "critical",
    "float_days",
    "reason",
    "after",
)


def schedule_table(ctx: ReportContext, tasks: Sequence[ScheduledTask], name: str) -> Table:
    """One row per scheduled task."""
    keys = {t.issue_id: t.issue_key for t in ctx.plan.scheduled_tasks}
    rows = tuple(
        (
            t.issue_key,
            str(ctx.tasks[t.issue_id].summary) if t.issue_id in ctx.tasks else "",
            t.lane,
            t.start,
            t.end,
            round(t.effort_hours, 2),
            t.effort_source.value,
            "yes" if t.critical else "no",
            t.total_float_working_days,
            t.constraint_reason.value,
            keys.get(t.constraining_issue_id) if t.constraining_issue_id is not None else None,
        )
        for t in sorted(tasks, key=lambda t: (t.start, t.issue_key))
    )
    return Table(name=name, columns=SCHEDULE_COLUMNS, rows=rows)


def _gantt(ctx: ReportContext) -> ReportResult:
    plan = ctx.plan
    low = plan.forecast.method is ForecastMethod.MONTE_CARLO
    title = LOW_CONFIDENCE_TITLE if low else "Gantt"
    tasks = plan.scheduled_tasks
    critical = sum(1 for t in tasks if t.critical)
    last = max((t.end for t in tasks), default=plan.plan_date)
    forecast = plan.forecast
    summary = (
        f"{len(tasks)} open tasks scheduled; the last ends {last}.",
        f"{critical} tasks on the critical path.",
        f"Forecast ({forecast.method.value}): P85 {forecast.p85}, {forecast.confidence.value}.",
    )
    return ReportResult(
        report_id=ReportId.GANTT,
        title=title,
        tables=(schedule_table(ctx, tasks, "schedule"),),
        figure=gantt_figure(ctx, tasks, GANTT_TITLE),
        summary=summary,
        titled_figure=True,
    )


def _critical_path(ctx: ReportContext) -> ReportResult:
    path = ctx.plan.critical_paths[0] if ctx.plan.critical_paths else ()
    by_id = {t.issue_id: t for t in ctx.plan.scheduled_tasks}
    tasks = [by_id[i] for i in path if i in by_id]
    keys = " → ".join(t.issue_key for t in tasks)
    return ReportResult(
        report_id=ReportId.CRITICAL_PATH,
        title="Critical path",
        tables=(schedule_table(ctx, tasks, "critical_path"),),
        figure=gantt_figure(ctx, tasks, "Critical path", group_by=GroupBy.NONE),
        summary=(f"{len(tasks)} tasks: {keys}." if tasks else "No critical path.",),
        titled_figure=True,
    )


def _dependency_map(ctx: ReportContext) -> ReportResult:
    graph: nx.DiGraph[str] = nx.DiGraph()
    keys = {t.issue_id: t.issue_key for t in ctx.plan.scheduled_tasks}
    graph.add_nodes_from(sorted(keys.values()))
    rows: list[tuple[str, str, str]] = []
    for edge in ctx.plan.dependency_edges:
        graph.add_edge(keys[edge.predecessor], keys[edge.successor])
        rows.append((keys[edge.predecessor], keys[edge.successor], "board"))
    external: set[str] = set()
    for t in ctx.plan.scheduled_tasks:
        source = ctx.tasks.get(t.issue_id)
        for blocker in source.external_blockers if source else ():
            if blocker.status_category is not StatusCategory.DONE:
                external.add(blocker.issue_key)
                graph.add_edge(blocker.issue_key, t.issue_key)
                rows.append((blocker.issue_key, t.issue_key, "external"))
    positions: dict[str, tuple[float, float]] = {}
    for layer, nodes in enumerate(nx.topological_generations(graph)):
        for row, node in enumerate(sorted(nodes)):
            positions[node] = (float(layer), float(-row))
    figure = go.Figure()
    for a, b in graph.edges:
        (x0, y0), (x1, y1) = positions[a], positions[b]
        figure.add_annotation(
            x=x1,
            y=y1,
            ax=x0,
            ay=y0,
            xref="x",
            yref="y",
            axref="x",
            ayref="y",
            showarrow=True,
            arrowhead=3,
            arrowcolor="#777777",
            text="",
        )
    for group, symbol, color in (("board", "circle", "#1565c0"), ("external", "x", "#9e9e9e")):
        nodes = [n for n in sorted(graph.nodes) if (n in external) == (group == "external")]
        figure.add_trace(
            go.Scatter(
                x=[positions[n][0] for n in nodes],
                y=[positions[n][1] for n in nodes],
                mode="markers+text",
                text=nodes,
                textposition="top center",
                marker={"size": 14, "symbol": symbol, "color": color},
                name="external blocker" if group == "external" else "on board",
            )
        )
    figure.update_xaxes(visible=False)
    figure.update_yaxes(visible=False)
    return ReportResult(
        report_id=ReportId.DEPENDENCY_MAP,
        title="Dependency map",
        tables=(Table("dependencies", ("blocker", "blocked", "kind"), tuple(sorted(rows))),),
        figure=base_layout(figure, "Dependency map", ctx.chart.theme),
        summary=(f"{len(rows)} dependencies, {len(external)} external blockers.",),
    )


def _milestones(ctx: ReportContext) -> ReportResult:
    rows = tuple(
        (
            m.kind.value,
            m.milestone_id,
            str(m.name),
            m.target_date,
            m.forecast_date,
            m.slip_working_days,
        )
        for m in ctx.plan.milestones
    )
    table = Table("milestones", ("kind", "id", "name", "target", "forecast", "slip_days"), rows)
    frame = pd.DataFrame(
        {
            "milestone": [html.escape(f"{r[2]} ({r[0]} {r[1]})") for r in rows],
            "slip_days": [r[5] or 0.0 for r in rows],
            "status": ["late" if (r[5] or 0) > 0 else "on time" for r in rows],
        }
    )
    figure = px.bar(
        frame,
        x="milestone",
        y="slip_days",
        color="status",
        color_discrete_map={"late": "#d32f2f", "on time": "#2e7d32"},
        category_orders={"status": ["late", "on time"]},
    )
    late = [r for r in rows if (r[5] or 0) > 0]
    return ReportResult(
        report_id=ReportId.MILESTONES,
        title="Milestones",
        tables=(table,),
        figure=base_layout(figure, "Milestone slip (working days)", ctx.chart.theme),
        summary=(f"{len(late)} of {len(rows)} milestones forecast late.",),
    )


gantt = Report(ReportId.GANTT, "Gantt / sequence view", "1.0", _gantt)
critical_path = Report(ReportId.CRITICAL_PATH, "Critical path", "1.0", _critical_path)
dependency_map = Report(ReportId.DEPENDENCY_MAP, "Dependency map", "1.0", _dependency_map)
milestones = Report(ReportId.MILESTONES, "Milestones", "1.0", _milestones)
