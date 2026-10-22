"""Plan → tool outputs. Pure functions; conclusions, not raw data (DESIGN §13.3)."""

from collections import Counter
from collections.abc import Mapping, Sequence

from pulseboard.domain.plan import Forecast, MilestoneForecast, Plan, PlanWarning
from pulseboard.domain.snapshot import BoardSnapshot, Task
from pulseboard.domain.types import DetailLevel, IssueId, StatusCategory
from pulseboard.mcp.models import (
    BuildPlanOut,
    CriticalTaskOut,
    FlowSummary,
    ForecastLine,
    LoadOut,
    MilestoneOut,
    PlanSummaryOut,
    WarningOut,
)
from pulseboard.services.plan_service import FlowMetrics, Throughput, WeekLoad, week_label

TOP_WARNINGS = {DetailLevel.SUMMARY: 3, DetailLevel.STANDARD: 10, DetailLevel.FULL: 25}
TOP_MILESTONES = {DetailLevel.SUMMARY: 3, DetailLevel.STANDARD: 10, DetailLevel.FULL: 25}
MAX_AGING_OUTLIERS = 5


def issue_keys(snapshots: Sequence[BoardSnapshot]) -> dict[IssueId, str]:
    """Issue ID → key for every issue on the snapshots."""
    return {t.issue_id: t.issue_key for s in snapshots for t in s.tasks}


def warning_out(warning: PlanWarning, keys: Mapping[IssueId, str]) -> WarningOut:
    """A warning with issue keys."""
    return WarningOut(
        rule=warning.rule_id,
        level=warning.level,
        message=warning.message,
        issues=[keys.get(i, str(i)) for i in warning.issue_ids],
    )


def milestone_out(milestone: MilestoneForecast, include_names: bool = True) -> MilestoneOut:
    """A milestone line; Jira names are dropped when ``include_names`` is off."""
    return MilestoneOut(
        kind=milestone.kind,
        id=milestone.milestone_id,
        name=milestone.name if include_names else None,
        target=milestone.target_date,
        forecast=milestone.forecast_date,
        slip_days=milestone.slip_working_days,
    )


def at_risk(plan: Plan, include_names: bool = True) -> list[MilestoneOut]:
    """Milestones forecast after their target, latest slip first."""
    late = [
        m for m in plan.milestones if m.slip_working_days is not None and m.slip_working_days > 0
    ]
    late.sort(key=lambda m: (-(m.slip_working_days or 0), m.milestone_id))
    return [milestone_out(m, include_names) for m in late]


def flow_summary(
    flow: FlowMetrics, throughput: Throughput, keys: Mapping[IssueId, str], window: int
) -> FlowSummary:
    """Flow metrics in a few numbers."""
    percentiles = flow.cycle_time_percentiles
    outliers = sorted(flow.aging_outliers().items(), key=lambda item: (-item[1], item[0]))
    notes = []
    if not percentiles:
        notes.append(
            f"No cycle time: no issue went from in progress to done in the {window}-day window."
        )
    if not throughput.weekly:
        notes.append("No throughput: the board has no issues yet.")
    if throughput.capped_from is not None:
        notes.append(
            f"Throughput covers {throughput.weeks} weeks, capped from {throughput.capped_from} "
            f"by the {throughput.window_days}-day history window."
        )
    return FlowSummary(
        window_days=window,
        throughput_per_week=list(throughput.weekly),
        cycle_time_p50_days=percentiles[0] if percentiles else None,
        cycle_time_p85_days=percentiles[1] if percentiles else None,
        aging_outliers=[keys.get(i, str(i)) for i, _ in outliers[:MAX_AGING_OUTLIERS]],
        note=" ".join(notes) or None,
    )


def build_plan_out(
    plan: Plan, keys: Mapping[IssueId, str], detail: DetailLevel, include_summaries: bool = True
) -> BuildPlanOut:
    """The ``pb_build_plan`` result."""
    return BuildPlanOut(
        plan_id=plan.plan_id,
        forecast_p50=plan.forecast.p50,
        forecast_p85=plan.forecast.p85,
        method=plan.forecast.method,
        confidence=plan.forecast.confidence,
        confidence_reason=plan.forecast.confidence_reason,
        solver_status=plan.solver_status,
        open_issues=len(plan.scheduled_tasks),
        critical_issues=sum(1 for t in plan.scheduled_tasks if t.critical),
        warning_count=len(plan.warnings),
        top_warnings=[warning_out(w, keys) for w in plan.warnings[: TOP_WARNINGS[detail]]],
        milestones_at_risk=(
            at_risk(plan, include_summaries)[: TOP_MILESTONES[detail]]
            if detail is not DetailLevel.SUMMARY
            else []
        ),
        stale_since=plan.stale_since,
    )


def _blocked(tasks: Sequence[Task], open_ids: set[IssueId]) -> int:
    def is_blocked(task: Task) -> bool:
        return any(b in open_ids for b in task.blocked_by) or any(
            e.status_category is not StatusCategory.DONE for e in task.external_blockers
        )

    return sum(1 for t in tasks if t.issue_id in open_ids and is_blocked(t))


def plan_summary_out(
    plan: Plan, snapshots: Sequence[BoardSnapshot], detail: DetailLevel
) -> PlanSummaryOut:
    """The ``pb_plan_summary`` result."""
    tasks = [t for s in snapshots for t in s.tasks]
    counts = Counter(t.status_category for t in tasks)
    open_ids = {t.issue_id for t in tasks if t.status_category is not StatusCategory.DONE}
    targets = [m for m in plan.milestones if m.target_date is not None]
    last = max(targets, key=lambda m: (m.target_date, m.milestone_id), default=None)
    keys = issue_keys(snapshots)
    return PlanSummaryOut(
        plan_id=plan.plan_id,
        plan_date=plan.plan_date,
        to_do=counts[StatusCategory.TO_DO],
        in_progress=counts[StatusCategory.IN_PROGRESS],
        done=counts[StatusCategory.DONE],
        blocked=_blocked(tasks, open_ids),
        forecast_p50=plan.forecast.p50,
        forecast_p85=plan.forecast.p85,
        method=plan.forecast.method,
        confidence=plan.forecast.confidence,
        last_milestone=milestone_out(last) if last else None,
        top_warnings=[warning_out(w, keys) for w in plan.warnings[: TOP_WARNINGS[detail]]],
        milestones_at_risk=at_risk(plan)[: TOP_MILESTONES[detail]],
    )


def critical_path_out(
    plan: Plan, snapshots: Sequence[BoardSnapshot], include_summaries: bool
) -> list[CriticalTaskOut]:
    """The longest critical path, in order; empty if nothing is critical."""
    if not plan.critical_paths:
        return []
    scheduled = {t.issue_id: t for t in plan.scheduled_tasks}
    summaries = {t.issue_id: t.summary for s in snapshots for t in s.tasks}
    keys = issue_keys(snapshots) | {t.issue_id: t.issue_key for t in plan.scheduled_tasks}
    return [
        CriticalTaskOut(
            key=scheduled[i].issue_key,
            summary=summaries.get(i) if include_summaries else None,
            start=scheduled[i].start,
            end=scheduled[i].end,
            assignee=scheduled[i].lane,
            reason=scheduled[i].constraint_reason,
            after=keys.get(c) if (c := scheduled[i].constraining_issue_id) is not None else None,
        )
        for i in plan.critical_paths[0]
    ]


def critical_path_note(plan: Plan) -> str:
    """Why the critical path is empty (used only when it is)."""
    return "Nothing is critical: the plan has no open scheduled work."


def load_out(load: WeekLoad) -> LoadOut:
    """A person-week line."""
    return LoadOut(
        account_id=load.account_id,
        week=week_label(load.week_start),
        allocated_h=round(load.allocated_hours, 2),
        available_h=round(load.available_hours, 2),
        utilization=round(load.utilization, 3),
    )


def forecast_line(forecast: Forecast) -> ForecastLine:
    """One forecast method's dates."""
    return ForecastLine(
        method=forecast.method,
        p50=forecast.p50,
        p85=forecast.p85,
        p95=forecast.p95,
        confidence=forecast.confidence,
        reason=forecast.confidence_reason,
    )
