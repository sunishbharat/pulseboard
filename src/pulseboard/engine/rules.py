"""The built-in warning rules (DESIGN §10.6). Registered as entry points in ``pyproject.toml``.

Messages name issues by key only; Jira free text never appears in a warning.
"""

from collections.abc import Iterator

from pulseboard.domain.types import (
    EffortSource,
    SprintState,
    StatusCategory,
    WarningId,
    WarningLevel,
)
from pulseboard.engine.calendar import local_date
from pulseboard.engine.warnings import Finding, WarningContext, WarningRule, week_label

_OVERALLOCATION_TOLERANCE = 1e-6


def _cycle(ctx: WarningContext) -> Iterator[Finding]:
    for edge in ctx.dropped_edges:
        a, b = ctx.key(edge.predecessor), ctx.key(edge.successor)
        yield (
            f"Dependency cycle: the link {a} → {b} was ignored.",
            (edge.predecessor, edge.successor),
        )


def _external_blocker(ctx: WarningContext) -> Iterator[Finding]:
    for task in ctx.tasks.values():
        for blocker in task.external_blockers:
            if blocker.status_category is not StatusCategory.DONE:
                message = f"{task.issue_key} is blocked by {blocker.issue_key}, not on the board."
                yield (message, (task.issue_id,))


def _missing_estimate(ctx: WarningContext) -> Iterator[Finding]:
    for issue_id, effort in ctx.efforts.items():
        if effort.source is EffortSource.DEFAULT:
            yield (f"{ctx.key(issue_id)} has no estimate; default effort used.", (issue_id,))


def _invalid_estimate(ctx: WarningContext) -> Iterator[Finding]:
    for task in ctx.tasks.values():
        if task.invalid_estimate:
            yield (f"{task.issue_key} has an estimate that could not be read.", (task.issue_id,))


def _unassigned(ctx: WarningContext) -> Iterator[Finding]:
    for task in ctx.tasks.values():
        if task.assignee is None:
            yield (f"{task.issue_key} has no assignee.", (task.issue_id,))


def _due_date_miss(ctx: WarningContext) -> Iterator[Finding]:
    for task in ctx.tasks.values():
        scheduled = ctx.scheduled.get(task.issue_id)
        if task.due_date and scheduled and scheduled.end > task.due_date:
            message = (
                f"{task.issue_key} is forecast to finish {scheduled.end}, after {task.due_date}."
            )
            yield (message, (task.issue_id,))


def _sprint_spill(ctx: WarningContext) -> Iterator[Finding]:
    for task in ctx.tasks.values():
        sprint = ctx.sprints.get(task.sprint_id) if task.sprint_id is not None else None
        scheduled = ctx.scheduled.get(task.issue_id)
        if sprint is None or scheduled is None or sprint.end is None:
            continue
        if sprint.state is not SprintState.CLOSED and scheduled.end > local_date(
            sprint.end, ctx.time_zone
        ):
            yield (
                f"{task.issue_key} is forecast to finish after its sprint ends.",
                (task.issue_id,),
            )


def _overallocation(ctx: WarningContext) -> Iterator[Finding]:
    for load in ctx.workload:
        if load.utilization > 1 + _OVERALLOCATION_TOLERANCE:
            percent = round(load.utilization * 100)
            yield (
                f"{load.account_id} is allocated {percent}% in {week_label(load.week_start)}.",
                (),
            )


def _blocked_in_progress(ctx: WarningContext) -> Iterator[Finding]:
    for task in ctx.tasks.values():
        if task.status_category is not StatusCategory.IN_PROGRESS:
            continue
        open_blockers = [ctx.key(b) for b in task.blocked_by if b in ctx.tasks]
        open_blockers += [
            b.issue_key
            for b in task.external_blockers
            if b.status_category is not StatusCategory.DONE
        ]
        if open_blockers:
            yield (
                f"{task.issue_key} is in progress but waits on {', '.join(sorted(open_blockers))}.",
                (task.issue_id,),
            )


def _milestone_risk(ctx: WarningContext) -> Iterator[Finding]:
    for milestone in ctx.milestones:
        if milestone.slip_working_days is not None and milestone.slip_working_days > 0:
            slip = milestone.slip_working_days
            name = f"{milestone.kind.value} {milestone.milestone_id}"
            yield (f"Milestone {name} is forecast {slip:g} working days late.", ())


def _low_confidence(ctx: WarningContext) -> Iterator[Finding]:
    if ctx.estimated_share < ctx.estimated_threshold:
        share = round(ctx.estimated_share * 100)
        yield (
            f"Only {share}% of open issues are estimated; the Monte Carlo forecast is primary.",
            (),
        )


def _solver_wallclock_cap(ctx: WarningContext) -> Iterator[Finding]:
    if ctx.solver_capped:
        yield ("The scheduler stopped at its wall-clock cap; the plan may vary between runs.", ())


def _stale_plan(ctx: WarningContext) -> Iterator[Finding]:
    if ctx.stale_since is not None:
        yield (f"Jira is unreachable; this plan is from {ctx.stale_since.isoformat()}.", ())


def _aging_wip(ctx: WarningContext) -> Iterator[Finding]:
    for issue_id, age in sorted(ctx.aging_outliers.items()):
        p85 = ctx.cycle_time_p85_days or 0.0
        yield (
            f"{ctx.key(issue_id)} has been in progress {age:g} days (cycle time P85: {p85:g}).",
            (issue_id,),
        )


W = WarningLevel.WARNING
cycle = WarningRule(WarningId.CYCLE, WarningLevel.ERROR, _cycle)
external_blocker = WarningRule(WarningId.EXTERNAL_BLOCKER, W, _external_blocker)
missing_estimate = WarningRule(WarningId.MISSING_ESTIMATE, W, _missing_estimate)
invalid_estimate = WarningRule(WarningId.INVALID_ESTIMATE, W, _invalid_estimate)
unassigned = WarningRule(WarningId.UNASSIGNED, W, _unassigned)
due_date_miss = WarningRule(WarningId.DUE_DATE_MISS, W, _due_date_miss)
sprint_spill = WarningRule(WarningId.SPRINT_SPILL, W, _sprint_spill)
overallocation = WarningRule(WarningId.OVERALLOCATION, W, _overallocation)
blocked_in_progress = WarningRule(WarningId.BLOCKED_IN_PROGRESS, W, _blocked_in_progress)
milestone_risk = WarningRule(WarningId.MILESTONE_RISK, W, _milestone_risk)
low_confidence = WarningRule(WarningId.LOW_CONFIDENCE, WarningLevel.INFO, _low_confidence)
solver_wallclock_cap = WarningRule(WarningId.SOLVER_WALLCLOCK_CAP, W, _solver_wallclock_cap)
stale_plan = WarningRule(WarningId.STALE_PLAN, W, _stale_plan)
aging_wip = WarningRule(WarningId.AGING_WIP, WarningLevel.INFO, _aging_wip)
