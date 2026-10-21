"""Status summary and workload / capacity reports (DESIGN §3, §14.1)."""

from collections import Counter

import numpy as np
import pandas as pd
import plotly.express as px

from pulseboard.domain.types import ReportId, StatusCategory
from pulseboard.engine.warnings import week_label
from pulseboard.reports.base import Report, ReportContext, ReportResult, Table, base_layout

STATUS_ORDER = (StatusCategory.TO_DO, StatusCategory.IN_PROGRESS, StatusCategory.DONE)
STATUS_COLORS = {"to_do": "#90a4ae", "in_progress": "#1e88e5", "done": "#43a047"}


def _status_summary(ctx: ReportContext) -> ReportResult:
    plan = ctx.plan
    counts = Counter(t.status_category for t in ctx.tasks.values())
    status = Table(
        "status",
        ("status_category", "issues"),
        tuple((c.value, counts[c]) for c in STATUS_ORDER),
    )
    by_rule = Counter(w.rule_id.value for w in plan.warnings)
    warnings = Table("warnings", ("rule", "count"), tuple(sorted(by_rule.items())))
    forecast = Table(
        "forecast",
        ("method", "p50", "p85", "p95", "confidence", "reason"),
        tuple(
            (f.method.value, f.p50, f.p85, f.p95, f.confidence.value, f.confidence_reason)
            for f in (plan.forecast, plan.secondary_forecast)
            if f is not None
        ),
    )
    frame = pd.DataFrame(
        {"status": [r[0] for r in status.rows], "issues": [r[1] for r in status.rows]}
    )
    figure = px.bar(
        frame,
        x="status",
        y="issues",
        color="status",
        color_discrete_map=STATUS_COLORS,
        category_orders={"status": [c.value for c in STATUS_ORDER]},
    )
    late = [m for m in plan.milestones if (m.slip_working_days or 0) > 0]
    primary = plan.forecast
    to_do, doing, done = (counts[c] for c in STATUS_ORDER)
    summary = (
        f"Forecast end {primary.p50} (P85 {primary.p85}), {primary.confidence.value} confidence.",
        f"{to_do} to do, {doing} in progress, {done} done.",
        f"{len(plan.warnings)} warnings; {len(late)} milestones at risk.",
    )
    return ReportResult(
        report_id=ReportId.STATUS_SUMMARY,
        title="Status summary",
        tables=(status, forecast, warnings),
        figure=base_layout(figure, "Issues by status", ctx.chart.theme),
        summary=summary,
    )


def _workload(ctx: ReportContext) -> ReportResult:
    loads = ctx.workload
    rows = tuple(
        (
            w.account_id,
            week_label(w.week_start),
            round(w.allocated_hours, 2),
            round(w.available_hours, 2),
            round(w.utilization, 3),
        )
        for w in loads
    )
    table = Table(
        "workload", ("account_id", "week", "allocated_h", "available_h", "utilization"), rows
    )
    people = sorted({r[0] for r in rows})
    weeks = sorted({r[1] for r in rows})
    matrix = np.full((len(people), len(weeks)), np.nan)
    for account, week, _, _, utilization in rows:
        matrix[people.index(account), weeks.index(week)] = utilization
    figure = px.imshow(
        matrix,
        x=weeks,
        y=people,
        zmin=0,
        zmax=1.2,
        color_continuous_scale="RdYlGn_r",
        text_auto=".0%",
        aspect="auto",
        labels={"color": "utilization"},
    )
    peak = max(loads, key=lambda w: w.utilization, default=None)
    summary = (
        f"Peak load {peak.utilization:.0%} for {peak.account_id} in {week_label(peak.week_start)}."
        if peak
        else "No assigned work.",
    )
    return ReportResult(
        report_id=ReportId.WORKLOAD,
        title="Workload",
        tables=(table,),
        figure=base_layout(figure, "Utilization per person-week", ctx.chart.theme),
        summary=summary,
    )


status_summary = Report(ReportId.STATUS_SUMMARY, "Status summary", "1.0", _status_summary)
workload = Report(ReportId.WORKLOAD, "Workload / capacity", "1.0", _workload)
