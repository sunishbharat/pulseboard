"""Report plugins against hand-computed fixture values (DESIGN §3, §4.4, §14.1)."""

from datetime import UTC, date, datetime, timedelta

import plotly.graph_objects as go
import pytest

from factories import FLOW_PLAN_DATE, fixture_board, flow_board, snapshot, task
from planning import SETTINGS, plan_of
from pulseboard.domain.types import ForecastMethod, ReportId, StatusCategory
from pulseboard.domain.untrusted import UntrustedText
from pulseboard.reports.base import (
    ReportContext,
    ReportResult,
    build_context,
    load_reports,
)
from pulseboard.reports.schedule import LOW_CONFIDENCE_TITLE

V1_REPORTS = {
    ReportId.STATUS_SUMMARY, ReportId.GANTT, ReportId.CRITICAL_PATH, ReportId.MILESTONES,
    ReportId.DEPENDENCY_MAP, ReportId.WORKLOAD, ReportId.BURN, ReportId.CUMULATIVE_FLOW,
    ReportId.CYCLE_TIME, ReportId.THROUGHPUT, ReportId.AGING_WIP, ReportId.MONTE_CARLO,
}  # fmt: skip


@pytest.fixture(scope="module")
def ctx() -> ReportContext:
    board = fixture_board()
    return build_context(plan_of(board), [board], SETTINGS)


def run(ctx: ReportContext, report_id: ReportId) -> ReportResult:
    return load_reports()[report_id].build(ctx)


def test_registry_has_every_v1_report() -> None:
    assert set(load_reports()) == V1_REPORTS


@pytest.mark.parametrize("report_id", sorted(V1_REPORTS))
def test_every_report_builds_tables_figure_and_summary(
    ctx: ReportContext, report_id: ReportId
) -> None:
    result = run(ctx, report_id)

    assert result.report_id is report_id
    assert result.tables
    assert isinstance(result.figure, go.Figure)
    assert result.figure.to_json()
    assert 1 <= len(result.summary) <= 3


def test_status_summary_counts(ctx: ReportContext) -> None:
    status = run(ctx, ReportId.STATUS_SUMMARY).tables[0]

    assert status.rows == (("to_do", 7), ("in_progress", 2), ("done", 3))


def test_throughput_hand_computed(ctx: ReportContext) -> None:
    # Board created Oct 1, plan date Oct 19: three weeks; one resolution in each.
    table = run(ctx, ReportId.THROUGHPUT).tables[0]

    assert table.rows == ((date(2026, 9, 29), 1), (date(2026, 10, 6), 1), (date(2026, 10, 13), 1))


def test_burn_up_hand_computed(ctx: ReportContext) -> None:
    # Sprint 11 runs Oct 12 → today (Oct 19): six issues, 15 points; ABC-103 (2 points) done Oct 15.
    rows = run(ctx, ReportId.BURN).tables[0].rows

    assert [r[0] for r in rows] == [date(2026, 10, 12) + timedelta(days=n) for n in range(8)]
    assert {(r[1], r[3]) for r in rows} == {(6, 15.0)}
    assert [r[2] for r in rows] == [0, 0, 0, 1, 1, 1, 1, 1]
    assert rows[-1][4] == 2.0


def test_burn_up_undoes_later_scope_changes() -> None:
    from pulseboard.domain.snapshot import Sprint, SprintMembershipChange
    from pulseboard.domain.types import IssueId, SprintId, SprintState

    sprint = Sprint(
        sprint_id=SprintId(11),
        name=UntrustedText("S11"),
        state=SprintState.ACTIVE,
        start=datetime(2026, 10, 12, 9, tzinfo=UTC),
        end=datetime(2026, 10, 23, 17, tzinfo=UTC),
    )
    added = SprintMembershipChange(
        issue_id=IssueId(2), entry_id=1, sprint_id=SprintId(11), added=True,
        at=datetime(2026, 10, 14, 16, tzinfo=UTC),
    )  # fmt: skip
    board = snapshot(
        (task(1, sprint_id=SprintId(11)), task(2, sprint_id=SprintId(11))), sprints=(sprint,)
    )
    board = board.model_copy(update={"sprint_changes": (added,)})
    rows = run(build_context(plan_of(board), [board], SETTINGS), ReportId.BURN).tables[0].rows

    assert [r[1] for r in rows[:4]] == [1, 1, 2, 2]  # ABC-2 joined on Oct 14 (scope creep)


def test_workload_hand_computed(ctx: ReportContext) -> None:
    # Velocity: 21 points over 10 days × 2 people = 1.05 points/day → 3 points = 22.857 h.
    # Bob: ABC-105 Mon–Wed of W43 (40 h available), ABC-110 Mon–Wed of W44 (24 h available).
    rows = {(r[0], r[1]): r for r in run(ctx, ReportId.WORKLOAD).tables[0].rows}
    hours = 3 / 1.05 * 8

    assert rows[("bob", "2026-W43")][2] == pytest.approx(hours, abs=0.01)
    assert rows[("bob", "2026-W43")][4] == pytest.approx(hours / 40, abs=0.001)
    assert rows[("bob", "2026-W44")][4] == pytest.approx(hours / 24, abs=0.001)


def test_low_confidence_sequence_view() -> None:
    created = datetime(2026, 9, 1, tzinfo=UTC)
    done = [
        task(
            n,
            category=StatusCategory.DONE,
            created=created,
            resolved=created + timedelta(days=7 * n),
        )
        for n in range(1, 6)
    ]
    open_work = [task(n, created=created) for n in range(10, 15)]
    board = snapshot((*done, *open_work))
    plan = plan_of(board)

    assert plan.forecast.method is ForecastMethod.MONTE_CARLO
    assert run(build_context(plan, [board], SETTINGS), ReportId.GANTT).title == LOW_CONFIDENCE_TITLE


def test_dependency_map_edges(ctx: ReportContext) -> None:
    rows = run(ctx, ReportId.DEPENDENCY_MAP).tables[0].rows

    assert rows == (
        ("ABC-104", "ABC-106", "board"),
        ("ABC-105", "ABC-107", "board"),
        ("ABC-106", "ABC-109", "board"),
        ("ABC-107", "ABC-109", "board"),
        ("ABC-109", "ABC-112", "board"),
    )


def test_milestones_table(ctx: ReportContext) -> None:
    rows = run(ctx, ReportId.MILESTONES).tables[0].rows

    assert rows == (("fix_version", 300, "1.0", date(2026, 10, 30), date(2026, 11, 2), 1.0),)


def test_monte_carlo_matches_the_plan_forecast(ctx: ReportContext) -> None:
    rows = run(ctx, ReportId.MONTE_CARLO).tables[0].rows
    forecast = ctx.plan.secondary_forecast
    assert forecast is not None

    assert [r[2] for r in rows] == [forecast.p50, forecast.p85, forecast.p95]


def test_cycle_time_and_aging_hand_computed() -> None:
    board = flow_board()
    plan = plan_of(board)
    context = build_context(
        plan.model_copy(update={"plan_date": FLOW_PLAN_DATE}), [board], SETTINGS
    )

    assert run(context, ReportId.CYCLE_TIME).tables[0].rows == (
        ("ABC-1", date(2026, 10, 15), 3.25),
    )
    aging = run(context, ReportId.AGING_WIP).tables[0].rows
    assert [(r[0], r[2], r[3]) for r in aging] == [("ABC-4", 7, "yes"), ("ABC-2", 3, "no")]
