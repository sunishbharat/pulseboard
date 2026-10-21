"""Analyzer: float, tight edges, critical paths, constraint reasons (DESIGN §10.5; §18.1 #13)."""

from scheduling_helpers import schedule

from factories import fixture_board, snapshot, task
from pulseboard.domain.types import AccountId, ConstraintReason, IssueId, SolverStatus
from pulseboard.engine.analyzer import analyze
from pulseboard.engine.scheduling import (
    Edge,
    Mode,
    ScheduleRequest,
    ScheduleResult,
    SchedulerTask,
    SolverBudget,
    TaskTiming,
)

DAY = 8.0
BUDGET = SolverBudget(1.0, 10.0, 0)


def request(*tasks: SchedulerTask, deps: tuple[Edge, ...] = ()) -> ScheduleRequest:
    return ScheduleRequest(tasks, (), deps, (), 1000, BUDGET)


def timing(i: int, start: int, end: int, who: str | None = "a") -> TaskTiming:
    return TaskTiming(IssueId(i), start, end, AccountId(who) if who else None)


def st(i: int, release: int = 0) -> SchedulerTask:
    return SchedulerTask(IssueId(i), (Mode(None, 1),), release)


def test_13_critical_path_includes_a_resource_bound_tight_edge() -> None:
    s = schedule(
        snapshot((task(1, original_estimate_hours=DAY), task(2, original_estimate_hours=DAY)))
    )
    a = analyze(s.built.request, s.result, max_paths=10)

    assert a.critical_paths == ((1, 2),)
    assert a.tasks[IssueId(2)].constraint_reason is ConstraintReason.RESOURCE
    assert a.tasks[IssueId(2)].constraining_issue_id == 1


def test_float_of_a_short_parallel_branch() -> None:
    # 1 -> 3 and 2 -> 3; task 2 is shorter, so it has float.
    req = request(
        st(1), st(2), st(3), deps=(Edge(IssueId(1), IssueId(3)), Edge(IssueId(2), IssueId(3)))
    )
    result = ScheduleResult(
        SolverStatus.OPTIMAL,
        (timing(1, 0, 4, "a"), timing(2, 0, 2, "b"), timing(3, 4, 6, "c")),
    )
    a = analyze(req, result, max_paths=10)

    assert a.tasks[IssueId(2)].total_float == 2
    assert not a.tasks[IssueId(2)].critical
    assert a.critical_paths == ((1, 3),)
    assert a.tasks[IssueId(3)].constraint_reason is ConstraintReason.DEPENDENCY
    assert a.tasks[IssueId(3)].constraining_issue_id == 1
    assert a.tasks[IssueId(1)].constraint_reason is ConstraintReason.PLAN_DATE
    assert a.plan_end == 6


def test_several_critical_paths_longest_first() -> None:
    req = request(
        st(1),
        st(2),
        st(3),
        st(4),
        deps=(Edge(IssueId(1), IssueId(2)), Edge(IssueId(3), IssueId(4))),
    )
    result = ScheduleResult(
        SolverStatus.OPTIMAL,
        (timing(1, 0, 2, "a"), timing(2, 2, 6, "b"), timing(3, 0, 5, "c"), timing(4, 5, 6, "d")),
    )
    a = analyze(req, result, max_paths=10)

    assert a.critical_paths == ((1, 2), (3, 4))


def test_path_limit() -> None:
    req = request(st(1), st(2))
    result = ScheduleResult(SolverStatus.OPTIMAL, (timing(1, 0, 2, "a"), timing(2, 0, 2, "b")))

    assert len(analyze(req, result, max_paths=1).critical_paths) == 1


def test_release_date_reason() -> None:
    req = request(st(1, release=10))
    result = ScheduleResult(SolverStatus.OPTIMAL, (timing(1, 10, 12),))

    assert (
        analyze(req, result, 10).tasks[IssueId(1)].constraint_reason
        is ConstraintReason.RELEASE_DATE
    )


def test_late_start_without_tight_predecessor_is_resource() -> None:
    req = request(st(1))
    result = ScheduleResult(SolverStatus.OPTIMAL, (timing(1, 4, 6),))

    assert analyze(req, result, 10).tasks[IssueId(1)].constraint_reason is ConstraintReason.RESOURCE


def test_empty_schedule() -> None:
    a = analyze(request(), ScheduleResult(SolverStatus.OPTIMAL, ()), 10)

    assert a.plan_end == 0
    assert a.critical_paths == ()


def test_fixture_board_float_is_never_negative() -> None:
    s = schedule(fixture_board())
    a = analyze(s.built.request, s.result, 10)

    assert all(t.total_float >= 0 for t in a.tasks.values())
    assert a.critical_paths
    assert all(a.tasks[i].critical for path in a.critical_paths for i in path)
