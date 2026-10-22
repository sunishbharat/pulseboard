"""Builds an immutable ``Plan`` from board snapshots: calendar → schedule → analyze → forecast.

Pure: the plan date, plan ID, generation time and engine version are all inputs.
"""

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from pulseboard.domain.plan import DependencyEdge, Forecast, Plan, ScheduledTask
from pulseboard.domain.snapshot import BoardSnapshot, Person
from pulseboard.domain.types import (
    Confidence,
    EffortSource,
    ForecastMethod,
    Hours,
    IssueId,
    PlanId,
    jira_name_key,
)
from pulseboard.engine.analyzer import Analysis, analyze
from pulseboard.engine.calendar import WorkCalendar, calendar_from_settings, unknown_people
from pulseboard.engine.effort import (
    Effort,
    PointsRate,
    RateSource,
    effort_for,
    estimated_share,
    points_rate,
)
from pulseboard.engine.flow import FlowMetrics, flow_metrics
from pulseboard.engine.forecaster import (
    Throughput,
    choose_primary,
    monte_carlo_forecast,
    weekly_throughput,
)
from pulseboard.engine.milestones import milestone_forecasts
from pulseboard.engine.request_builder import BuiltRequest, build_request, merged_open_tasks
from pulseboard.engine.scheduling import Scheduler, ScheduleResult
from pulseboard.engine.warnings import WarningContext, WarningRule, run_rules
from pulseboard.engine.workload import workload
from pulseboard.settings import Settings, TeamSettings


@dataclass(frozen=True)
class PlanRequest:
    """Inputs for one plan."""

    snapshots: tuple[BoardSnapshot, ...]
    settings: Settings
    plan_date: date
    plan_id: PlanId
    engine_version: str
    settings_hash: str
    generated_at: datetime
    seed: int
    """Monte Carlo seed, derived from ``plan_id`` by the caller."""
    stale_since: datetime | None = None


@dataclass(frozen=True)
class Stages:
    """Intermediate results, for reports that need more than the plan."""

    calendar: WorkCalendar
    efforts: Mapping[IssueId, Effort]
    built: BuiltRequest
    result: ScheduleResult
    analysis: Analysis
    flow: FlowMetrics
    throughput: Throughput
    rate: PointsRate


def combined_snapshot_hash(snapshots: Sequence[BoardSnapshot]) -> str:
    """The snapshot hash, or a hash of all hashes for several boards."""
    if len(snapshots) == 1:
        return snapshots[0].snapshot_hash
    joined = "\n".join(sorted(s.snapshot_hash for s in snapshots))
    return hashlib.sha256(joined.encode()).hexdigest()


def _scheduled_tasks(stages: Stages) -> dict[IssueId, ScheduledTask]:
    cal = stages.calendar
    tentative = {t.issue_id for t in stages.built.request.tasks if t.tentative}
    scheduled = {}
    for issue_id, item in stages.analysis.tasks.items():
        timing = item.timing
        effort = stages.efforts[issue_id]
        scheduled[issue_id] = ScheduledTask(
            issue_id=issue_id,
            issue_key=stages.built.tasks[issue_id].issue_key,
            start=cal.date_of_unit(timing.start),
            end=cal.end_date(timing.end),
            effort_hours=Hours(effort.hours),
            duration_working_days=cal.units_to_working_days(timing.end - timing.start),
            effort_source=effort.source,
            lane=timing.resource,
            constraint_reason=item.constraint_reason,
            constraining_issue_id=item.constraining_issue_id,
            total_float_working_days=cal.units_to_working_days(item.total_float),
            critical=item.critical,
            tentative=issue_id in tentative,
        )
    return scheduled


_LOWER = {Confidence.HIGH: Confidence.MEDIUM, Confidence.MEDIUM: Confidence.LOW}


def people_of(snapshots: Sequence[BoardSnapshot]) -> tuple[Person, ...]:
    """Every person on the snapshots, by identity."""
    return tuple({p.account_id: p for s in snapshots for p in s.people}.values())


def unknown_link_types(snapshots: Sequence[BoardSnapshot], team: TeamSettings) -> tuple[str, ...]:
    """Configured dependency link types that match no link on the board(s) (warning W015)."""
    seen = {jira_name_key(name) for s in snapshots for name in s.link_types}
    return tuple(t.name for t in team.dependency_link_types if jira_name_key(t.name) not in seen)


def fallback_rate(rate: PointsRate, efforts: Mapping[IssueId, Effort]) -> float | None:
    """The fallback points rate, when it converted any open task's story points (W017)."""
    pointed = any(e.source is EffortSource.STORY_POINTS for e in efforts.values())
    return rate.per_day if pointed and rate.source is RateSource.FALLBACK else None


def schedule_forecast(
    end: date,
    share: float,
    team: TeamSettings,
    solved: bool,
    points_fallback: float | None = None,
) -> Forecast:
    """The deterministic schedule as a forecast, with confidence from the estimated share.

    Story points converted at the fallback rate (``points_fallback``) lower the confidence one
    step, and the reason says so (ADDENDUM-03 §6).
    """
    percent = round(share * 100)
    if not solved:
        confidence, reason = Confidence.LOW, "the scheduler found no solution"
    elif share >= team.high_confidence_threshold:
        confidence, reason = Confidence.HIGH, f"{percent}% of open issues are estimated"
    elif share >= team.estimated_threshold:
        confidence, reason = Confidence.MEDIUM, f"{percent}% of open issues are estimated"
    else:
        confidence, reason = Confidence.LOW, f"only {percent}% of open issues are estimated"
    if solved and points_fallback is not None:
        confidence = _LOWER.get(confidence, Confidence.LOW)
        reason += (
            f"; story points use the fallback rate of {points_fallback:g} points per day "
            "(no closed sprint with completed points)"
        )
    return Forecast(
        method=ForecastMethod.SCHEDULE,
        p50=end,
        p85=end,
        p95=end,
        confidence=confidence,
        confidence_reason=reason,
    )


def run_stages(request: PlanRequest, scheduler: Scheduler) -> Stages:
    """Calendar, effort, request and solve, and analysis."""
    team = request.settings.team
    limits = request.settings.limits
    calendar = calendar_from_settings(
        request.plan_date, team, limits.planning_horizon_years, people_of(request.snapshots)
    )
    rate = points_rate(request.snapshots[0], calendar, team.points, team.time_zone)
    efforts = {
        issue_id: effort_for(
            task,
            points_per_day=rate.per_day,
            hours_per_day=team.hours_per_day,
            default_effort_hours=team.default_effort_hours,
        )
        for issue_id, task in merged_open_tasks(request.snapshots).items()
    }
    built = build_request(request.snapshots, efforts, calendar, team)
    result = scheduler.solve(built.request)
    analysis = analyze(built.request, result, limits.max_critical_paths)
    all_tasks = tuple({t.issue_id: t for snap in request.snapshots for t in snap.tasks}.values())
    flow = flow_metrics(
        request.snapshots[0], request.plan_date, team.time_zone, limits.reporting_window_days
    )
    throughput = weekly_throughput(
        all_tasks,
        request.plan_date,
        team.time_zone,
        limits.throughput_history_weeks,
        limits.reporting_window_days,
    )
    return Stages(calendar, efforts, built, result, analysis, flow, throughput, rate)


def build_plan(
    request: PlanRequest, scheduler: Scheduler, rules: Sequence[WarningRule]
) -> tuple[Plan, Stages]:
    """Build the plan and return it with its intermediate stages."""
    settings = request.settings
    team = settings.team
    stages = run_stages(request, scheduler)
    scheduled = _scheduled_tasks(stages)
    share = estimated_share(stages.efforts)
    solved = bool(stages.result.timings) or not stages.built.tasks
    end = max((s.end for s in scheduled.values()), default=stages.calendar.origin)
    monte_carlo = monte_carlo_forecast(
        len(stages.built.tasks),
        stages.throughput,
        stages.calendar,
        trials=settings.limits.monte_carlo_trials,
        seed=request.seed,
        min_history_weeks=settings.limits.min_history_weeks,
    )
    points_fallback = fallback_rate(stages.rate, stages.efforts)
    forecast, secondary = choose_primary(
        schedule_forecast(end, share, team, solved, points_fallback),
        monte_carlo,
        share,
        team.estimated_threshold,
    )
    milestones = milestone_forecasts(request.snapshots, scheduled, stages.calendar)
    loads = workload(
        {t.issue_id: t for t in stages.result.timings},
        stages.efforts,
        stages.calendar,
        team.hours_per_day,
    )
    context = WarningContext(
        tasks=stages.built.tasks,
        all_tasks={t.issue_id: t for snap in request.snapshots for t in snap.tasks},
        efforts=stages.efforts,
        scheduled=scheduled,
        sprints={s.sprint_id: s for snap in request.snapshots for s in snap.sprints},
        time_zone=team.time_zone,
        dropped_edges=stages.built.dropped_edges,
        milestones=milestones,
        workload=loads,
        estimated_share=share,
        estimated_threshold=team.estimated_threshold,
        solver_capped=stages.result.wallclock_capped,
        stale_since=request.stale_since,
        aging_outliers=stages.flow.aging_outliers(),
        cycle_time_p85_days=stages.flow.cycle_time_p85,
        unknown_link_types=unknown_link_types(request.snapshots, team),
        link_types_seen=tuple(sorted({n for s in request.snapshots for n in s.link_types})),
        unknown_people=unknown_people(team, people_of(request.snapshots)),
        points_fallback_rate=points_fallback,
        monte_carlo_missing=monte_carlo is None,
    )
    first = request.snapshots[0]
    plan = Plan(
        plan_id=request.plan_id,
        site_id=first.site_id,
        board_ids=tuple(s.board_id for s in request.snapshots),
        account_id=first.account_id,
        plan_date=request.plan_date,
        team_time_zone=team.time_zone,
        engine_version=request.engine_version,
        settings_hash=request.settings_hash,
        snapshot_hash=combined_snapshot_hash(request.snapshots),
        scheduled_tasks=tuple(scheduled.values()),
        dependency_edges=tuple(
            DependencyEdge(predecessor=e.predecessor, successor=e.successor)
            for e in stages.built.request.dependencies
        ),
        critical_paths=stages.analysis.critical_paths,
        critical_paths_truncated=stages.analysis.paths_truncated,
        forecast=forecast,
        secondary_forecast=secondary,
        estimated_share=round(share, 6),
        milestones=milestones,
        warnings=run_rules(rules, context, settings.disabled_warnings),
        solver_status=stages.result.status,
        generated_at=request.generated_at,
        stale_since=request.stale_since,
    )
    return plan, stages
