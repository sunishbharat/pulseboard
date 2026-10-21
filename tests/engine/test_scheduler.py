"""Scheduler cases of DESIGN §18.1 (#1–#12, #14–#18; #13 is an analyzer case, M3.4)."""

import json
from dataclasses import asdict
from datetime import UTC, date, datetime

from scheduling_helpers import schedule
from syrupy.assertion import SnapshotAssertion

from factories import fixture_board, snapshot, task
from pulseboard.domain.snapshot import ExternalBlocker, Sprint
from pulseboard.domain.types import (
    AccountId,
    IssueId,
    IssueKey,
    SolverStatus,
    SprintId,
    SprintState,
    StatusCategory,
)
from pulseboard.domain.untrusted import UntrustedText
from pulseboard.engine.pyjobshop_scheduler import PyJobShopScheduler
from pulseboard.engine.scheduling import (
    Edge,
    Mode,
    Resource,
    ScheduleRequest,
    SchedulerTask,
    SolverBudget,
)
from pulseboard.settings import (
    LeavePeriod,
    PersonSettings,
    PriorityMode,
    SolverSettings,
    TeamSettings,
    UnassignedPolicy,
)

DAY = 8.0  # hours: one working day = 2 units
IN_PROGRESS = StatusCategory.IN_PROGRESS


def test_01_chain_on_one_person_is_sequential() -> None:
    s = schedule(
        snapshot(
            (
                task(1, original_estimate_hours=DAY),
                task(2, original_estimate_hours=DAY, blocked_by=(IssueId(1),)),
                task(3, original_estimate_hours=DAY, blocked_by=(IssueId(2),)),
            )
        )
    )
    assert [s.span(i) for i in (1, 2, 3)] == [(0, 2), (2, 4), (4, 6)]
    assert s.result.status is SolverStatus.OPTIMAL


def test_02_independent_tasks_two_people_run_in_parallel() -> None:
    s = schedule(
        snapshot(
            (
                task(1, original_estimate_hours=DAY),
                task(2, original_estimate_hours=DAY, assignee="bob"),
            )
        )
    )
    assert s.span(1) == s.span(2) == (0, 2)


def test_03_independent_tasks_one_person_follow_board_order() -> None:
    s = schedule(
        snapshot(
            (
                task(1, original_estimate_hours=DAY, board_rank=5),
                task(2, original_estimate_hours=DAY, board_rank=1),
            )
        )
    )
    assert s.span(2) == (0, 2)
    assert s.span(1) == (2, 4)


def test_04_blocked_task_starts_when_blocker_ends() -> None:
    s = schedule(
        snapshot(
            (
                task(1, original_estimate_hours=3 * DAY),
                task(2, original_estimate_hours=DAY, assignee="bob", blocked_by=(IssueId(1),)),
            )
        )
    )
    assert s.span(2) == (6, 8)


def test_05_weekend_inside_a_task_is_skipped() -> None:
    s = schedule(
        snapshot((task(1, original_estimate_hours=3 * DAY),)), plan_date=date(2026, 10, 22)
    )

    assert s.calendar.date_of_unit(s.timing(1).start) == date(2026, 10, 22)  # Thursday
    assert s.calendar.end_date(s.timing(1).end) == date(2026, 10, 26)  # Monday


def test_06_public_holiday_inside_a_task_pushes_it() -> None:
    s = schedule(
        snapshot((task(1, original_estimate_hours=2 * DAY),)), plan_date=date(2026, 11, 25)
    )

    assert s.calendar.end_date(s.timing(1).end) == date(2026, 11, 27)  # Thanksgiving skipped


def test_07_cycle_drops_edge_into_lowest_ranked_task() -> None:
    s = schedule(
        snapshot(
            (
                task(1, original_estimate_hours=DAY, blocked_by=(IssueId(2),)),
                task(2, original_estimate_hours=DAY, assignee="bob", blocked_by=(IssueId(1),)),
            )
        )
    )
    assert s.built.dropped_edges == (Edge(IssueId(1), IssueId(2)),)
    assert s.span(2) == (0, 2)
    assert s.span(1) == (2, 4)


def test_08_half_capacity_doubles_duration() -> None:
    team = TeamSettings(people=(PersonSettings(account_id=AccountId("bob"), capacity=0.5),))
    s = schedule(snapshot((task(1, original_estimate_hours=DAY, assignee="bob"),)), team=team)

    assert s.span(1) == (0, 4)


def test_09_future_sprint_task_waits_for_sprint_start() -> None:
    future = Sprint(
        sprint_id=SprintId(9),
        name=UntrustedText("Next"),
        state=SprintState.FUTURE,
        start=datetime(2026, 10, 26, 9, tzinfo=UTC),
        end=datetime(2026, 11, 6, 17, tzinfo=UTC),
    )
    s = schedule(
        snapshot((task(1, original_estimate_hours=DAY, sprint_id=SprintId(9)),), sprints=(future,))
    )
    assert s.timing(1).start == 10
    assert s.calendar.date_of_unit(10) == date(2026, 10, 26)


def test_10_in_progress_task_starts_on_plan_date_with_remaining_effort() -> None:
    s = schedule(
        snapshot(
            (
                task(1, original_estimate_hours=DAY, board_rank=0),
                task(
                    2,
                    category=IN_PROGRESS,
                    original_estimate_hours=5 * DAY,
                    remaining_estimate_hours=4.0,
                    board_rank=9,
                ),
            )
        )
    )
    assert s.span(2) == (0, 1)
    assert s.span(1) == (1, 3)


def test_11_unestimated_task_gets_default_effort() -> None:
    s = schedule(snapshot((task(1),)), team=TeamSettings(default_effort_hours=12.0))

    assert s.span(1) == (0, 3)


def test_12_external_open_blocker_does_not_stop_scheduling() -> None:
    blocker = ExternalBlocker(
        issue_id=IssueId(900), issue_key=IssueKey("XYZ-9"), status_category=StatusCategory.TO_DO
    )
    s = schedule(snapshot((task(1, original_estimate_hours=DAY, external_blockers=(blocker,)),)))

    assert s.span(1) == (0, 2)


def test_14_same_input_gives_identical_result(snapshot: SnapshotAssertion) -> None:
    board = fixture_board()
    first = schedule(board).result
    second = schedule(board).result

    assert first == second
    assert json.dumps([asdict(t) for t in first.timings], indent=1) == snapshot


def test_15_board_order_yields_to_a_dependency() -> None:
    s = schedule(
        snapshot(
            (
                task(1, original_estimate_hours=DAY, board_rank=1, blocked_by=(IssueId(2),)),
                task(2, original_estimate_hours=DAY, board_rank=2),
            )
        )
    )
    assert s.span(2) == (0, 2)
    assert s.span(1) == (2, 4)
    assert s.built.request.sequence == ()


def test_16_auto_assign_picks_the_earliest_finisher_and_is_tentative() -> None:
    s = schedule(
        snapshot(
            (
                task(1, original_estimate_hours=2 * DAY),
                task(2, original_estimate_hours=2 * DAY, board_rank=2),
                task(3, original_estimate_hours=DAY, assignee=None),
                task(4, original_estimate_hours=DAY, assignee="bob"),
            )
        ),
        team=TeamSettings(unassigned_policy=UnassignedPolicy.AUTO_ASSIGN),
    )
    assert s.timing(3).resource == "bob"
    assert next(t for t in s.built.request.tasks if t.issue_id == 3).tentative
    assert s.span(1)[1] == 4
    assert s.span(2)[1] == 8


def test_unassigned_separate_lane_has_no_resource() -> None:
    s = schedule(snapshot((task(1, original_estimate_hours=DAY, assignee=None),)))

    assert s.timing(1).resource is None
    assert s.span(1) == (0, 2)


def test_optimized_mode_adds_no_rank_edges() -> None:
    team = TeamSettings(priority_mode=PriorityMode.OPTIMIZED)
    s = schedule(
        snapshot((task(1, original_estimate_hours=DAY), task(2, original_estimate_hours=DAY))),
        team=team,
    )

    assert s.built.request.sequence == ()
    assert sorted([s.span(1), s.span(2)]) == [(0, 2), (2, 4)]


def test_leave_pauses_a_task() -> None:
    team = TeamSettings(
        leave=(
            LeavePeriod(
                account_id=AccountId("alice"), start=date(2026, 10, 20), end=date(2026, 10, 20)
            ),
        )
    )
    s = schedule(snapshot((task(1, original_estimate_hours=2 * DAY),)), team=team)

    assert s.span(1) == (0, 6)  # Monday, (Tuesday off), Wednesday


def test_17_wallclock_cap_gives_feasible_plan_and_flag() -> None:
    tasks = tuple(
        SchedulerTask(
            IssueId(i), tuple(Mode(AccountId(p), 1 + (i * 7 + j) % 9) for j, p in enumerate("abc"))
        )
        for i in range(1, 61)
    )
    request = ScheduleRequest(
        tasks=tasks,
        resources=tuple(Resource(AccountId(p)) for p in "abc"),
        dependencies=tuple(Edge(IssueId(i), IssueId(i + 3)) for i in range(1, 58, 4)),
        sequence=(),
        horizon=10_000,
        budget=SolverBudget(deterministic_time=1_000.0, wallclock_cap_s=0.5, seed=0),
    )
    result = PyJobShopScheduler().solve(request)

    assert result.wallclock_capped
    assert result.status is SolverStatus.FEASIBLE
    assert len(result.timings) == 60


def test_18_program_view_never_double_books_a_person() -> None:
    board_a = snapshot((task(1, original_estimate_hours=2 * DAY),))
    board_b = snapshot((task(2, original_estimate_hours=2 * DAY),))
    s = schedule(board_a, board_b)

    spans = sorted([s.span(1), s.span(2)])
    assert spans[0][1] <= spans[1][0]


def test_empty_request() -> None:
    s = schedule(snapshot(()))

    assert s.result.timings == ()
    assert s.result.status is SolverStatus.OPTIMAL


def test_solver_settings_reach_the_request() -> None:
    team = TeamSettings(solver=SolverSettings(deterministic_time=0.5, wallclock_cap_s=9.0, seed=4))
    s = schedule(snapshot((task(1),)), team=team)

    assert s.built.request.budget == SolverBudget(0.5, 9.0, 4)
