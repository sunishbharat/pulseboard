"""Each warning rule has its own test (DESIGN §10.6), plus the registry."""

from dataclasses import replace
from datetime import UTC, date, datetime

from factories import task
from pulseboard.domain.plan import MilestoneForecast, MilestoneKind, ScheduledTask
from pulseboard.domain.snapshot import ExternalBlocker, Sprint, Task
from pulseboard.domain.types import (
    AccountId,
    ConstraintReason,
    EffortSource,
    Hours,
    IssueId,
    IssueKey,
    SprintId,
    SprintState,
    StatusCategory,
    WarningId,
    WarningLevel,
    WorkingDays,
)
from pulseboard.domain.untrusted import UntrustedText
from pulseboard.engine import rules
from pulseboard.engine.effort import Effort
from pulseboard.engine.scheduling import Edge
from pulseboard.engine.warnings import WarningContext, WarningRule, load_rules, run_rules
from pulseboard.engine.workload import WeekLoad

EST = Effort(8.0, EffortSource.STORY_POINTS)


def ctx(*tasks: Task, **changes: object) -> WarningContext:
    by_id = {t.issue_id: t for t in tasks}
    base = WarningContext(
        tasks={i: t for i, t in by_id.items() if t.status_category is not StatusCategory.DONE},
        all_tasks=by_id,
        efforts={i: EST for i, t in by_id.items() if t.status_category is not StatusCategory.DONE},
        scheduled={},
        sprints={},
        time_zone="UTC",
    )
    return replace(base, **changes)  # type: ignore[arg-type]  # test helper forwards fields


def scheduled(issue_id: int, end: date) -> ScheduledTask:
    return ScheduledTask(
        issue_id=IssueId(issue_id),
        issue_key=IssueKey(f"ABC-{issue_id}"),
        start=date(2026, 10, 19),
        end=end,
        effort_hours=Hours(8.0),
        duration_working_days=WorkingDays(1.0),
        effort_source=EffortSource.STORY_POINTS,
        lane=AccountId("alice"),
        constraint_reason=ConstraintReason.PLAN_DATE,
        total_float_working_days=WorkingDays(0.0),
        critical=True,
    )


def ids(context: WarningContext, rule: WarningRule) -> list[tuple[int, ...]]:
    return [w.issue_ids for w in rule.evaluate(context)]


def test_w001_cycle() -> None:
    c = ctx(task(1), task(2), dropped_edges=(Edge(IssueId(1), IssueId(2)),))
    [warning] = rules.cycle.evaluate(c)

    assert warning.level is WarningLevel.ERROR
    assert warning.issue_ids == (1, 2)
    assert "ABC-1 → ABC-2" in warning.message


def test_w002_external_blocker() -> None:
    open_blocker = ExternalBlocker(
        issue_id=IssueId(9), issue_key=IssueKey("X-9"), status_category=StatusCategory.TO_DO
    )
    done_blocker = ExternalBlocker(
        issue_id=IssueId(8), issue_key=IssueKey("X-8"), status_category=StatusCategory.DONE
    )
    c = ctx(task(1, external_blockers=(open_blocker,)), task(2, external_blockers=(done_blocker,)))

    assert ids(c, rules.external_blocker) == [(1,)]


def test_w003_missing_estimate() -> None:
    c = ctx(
        task(1), task(2), efforts={IssueId(1): EST, IssueId(2): Effort(8.0, EffortSource.DEFAULT)}
    )

    assert ids(c, rules.missing_estimate) == [(2,)]


def test_w004_invalid_estimate() -> None:
    assert ids(ctx(task(1, invalid_estimate=True), task(2)), rules.invalid_estimate) == [(1,)]


def test_w005_unassigned() -> None:
    assert ids(ctx(task(1, assignee=None), task(2)), rules.unassigned) == [(1,)]


def test_w006_due_date_miss() -> None:
    c = ctx(
        task(1, due_date=date(2026, 10, 20)),
        task(2, due_date=date(2026, 10, 30)),
        scheduled={
            IssueId(1): scheduled(1, date(2026, 10, 21)),
            IssueId(2): scheduled(2, date(2026, 10, 21)),
        },
    )
    assert ids(c, rules.due_date_miss) == [(1,)]


def test_w007_sprint_spill() -> None:
    sprint = Sprint(
        sprint_id=SprintId(5),
        name=UntrustedText("S5"),
        state=SprintState.ACTIVE,
        start=datetime(2026, 10, 12, tzinfo=UTC),
        end=datetime(2026, 10, 23, 17, tzinfo=UTC),
    )
    c = ctx(
        task(1, sprint_id=SprintId(5)),
        task(2, sprint_id=SprintId(5)),
        scheduled={
            IssueId(1): scheduled(1, date(2026, 10, 26)),
            IssueId(2): scheduled(2, date(2026, 10, 23)),
        },
        sprints={SprintId(5): sprint},
    )
    assert ids(c, rules.sprint_spill) == [(1,)]


def test_w008_overallocation() -> None:
    week = date(2026, 10, 19)
    c = ctx(
        workload=(
            WeekLoad(AccountId("alice"), week, 48.0, 40.0),
            WeekLoad(AccountId("bob"), week, 40.0, 40.0),
        )
    )
    [warning] = rules.overallocation.evaluate(c)

    assert "alice is allocated 120% in 2026-W43" in warning.message


def test_w009_blocked_in_progress() -> None:
    doing = StatusCategory.IN_PROGRESS
    c = ctx(
        task(1),
        task(2, category=doing, blocked_by=(IssueId(1),)),
        task(3, category=doing, blocked_by=(IssueId(4),)),
        task(4, category=StatusCategory.DONE),
    )
    assert ids(c, rules.blocked_in_progress) == [(2,)]


def test_w010_milestone_risk() -> None:
    def milestone(slip: float | None) -> MilestoneForecast:
        return MilestoneForecast(
            kind=MilestoneKind.FIX_VERSION,
            milestone_id=300,
            name=UntrustedText("1.0"),
            target_date=date(2026, 11, 3),
            forecast_date=date(2026, 11, 5),
            slip_working_days=None if slip is None else WorkingDays(slip),
        )

    c = ctx(milestones=(milestone(2.0), milestone(0.0), milestone(None)))
    [warning] = rules.milestone_risk.evaluate(c)

    assert "2 working days late" in warning.message


def test_w011_low_confidence() -> None:
    assert (
        len(rules.low_confidence.evaluate(ctx(estimated_share=0.4, estimated_threshold=0.6))) == 1
    )
    assert rules.low_confidence.evaluate(ctx(estimated_share=0.6, estimated_threshold=0.6)) == []


def test_w012_solver_wallclock_cap() -> None:
    assert len(rules.solver_wallclock_cap.evaluate(ctx(solver_capped=True))) == 1
    assert rules.solver_wallclock_cap.evaluate(ctx()) == []


def test_w013_stale_plan() -> None:
    [warning] = rules.stale_plan.evaluate(ctx(stale_since=datetime(2026, 10, 19, 8, tzinfo=UTC)))

    assert "2026-10-19T08:00:00+00:00" in warning.message


def test_w014_aging_wip() -> None:
    c = ctx(task(1), aging_outliers={IssueId(1): 12.0}, cycle_time_p85_days=5.0)
    [warning] = rules.aging_wip.evaluate(c)

    assert warning.issue_ids == (1,)
    assert "12 days" in warning.message


def test_registry_loads_every_rule_from_entry_points() -> None:
    loaded = load_rules()

    assert [r.rule_id for r in loaded] == sorted(WarningId)


def test_run_rules_orders_and_disables() -> None:
    c = ctx(
        task(1, assignee=None),
        dropped_edges=(Edge(IssueId(1), IssueId(1)),),
        estimated_share=0.1,
        estimated_threshold=0.6,
    )
    loaded = load_rules()

    found = run_rules(loaded, c)
    assert [w.rule_id for w in found] == [
        WarningId.CYCLE,
        WarningId.UNASSIGNED,
        WarningId.LOW_CONFIDENCE,
    ]

    found = run_rules(loaded, c, disabled=[WarningId.UNASSIGNED])
    assert WarningId.UNASSIGNED not in {w.rule_id for w in found}
