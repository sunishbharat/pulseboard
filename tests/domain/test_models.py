"""Domain models: identity, time, immutability and stable hashing (DESIGN §7.2, §8)."""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from pulseboard.domain.plan import (
    PLAN_SCHEMA_VERSION,
    ExportRecord,
    Forecast,
    Plan,
    ScheduledTask,
)
from pulseboard.domain.snapshot import BoardColumn, BoardSnapshot, StatusTransition, Task
from pulseboard.domain.types import (
    AccountId,
    BoardId,
    BoardType,
    Confidence,
    ConstraintReason,
    EffortSource,
    EstimationMode,
    ExportFormat,
    ExportId,
    ForecastMethod,
    Hours,
    IssueId,
    IssueKey,
    PlanId,
    ReportId,
    SiteId,
    SolverStatus,
    StatusCategory,
    WorkingDays,
)
from pulseboard.domain.untrusted import UntrustedText

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def task(issue_id: int, key: str = "ABC-1", **changes: object) -> Task:
    fields: dict[str, object] = {
        "issue_id": IssueId(issue_id),
        "issue_key": IssueKey(key),
        "summary": UntrustedText("Do the thing"),
        "url": f"https://acme.atlassian.net/browse/{key}",
        "issue_type": UntrustedText("Story"),
        "status_id": "3",
        "status": UntrustedText("In Progress"),
        "status_category": StatusCategory.IN_PROGRESS,
        "board_rank": 0,
        "created": T0,
    }
    fields.update(changes)
    return Task.model_validate(fields)


def snapshot(
    tasks: tuple[Task, ...],
    fetched_at: datetime = T0,
    transitions: tuple[StatusTransition, ...] = (),
) -> BoardSnapshot:
    return BoardSnapshot(
        transitions=transitions,
        site_id=SiteId("acme"),
        board_id=BoardId(42),
        account_id=AccountId("acc-1"),
        board_name=UntrustedText("Team board"),
        board_type=BoardType.SCRUM,
        columns=(BoardColumn(name=UntrustedText("Doing"), status_ids=("3",)),),
        estimation_mode=EstimationMode.STORY_POINTS,
        tasks=tasks,
        fetched_at=fetched_at,
    )


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        task(1, created=datetime(2026, 10, 1, 9, 0))  # noqa: DTZ001 - the point of the test


def test_aware_datetime_normalized_to_utc() -> None:
    berlin = timezone(timedelta(hours=2))
    t = task(1, created=datetime(2026, 10, 1, 11, 0, tzinfo=berlin))

    assert t.created == T0
    assert t.created.utcoffset() == timedelta(0)


def test_models_are_frozen() -> None:
    t = task(1)
    with pytest.raises(ValidationError, match="frozen"):
        t.board_rank = 3


def test_strict_mode_rejects_coercion() -> None:
    with pytest.raises(ValidationError):
        task(1, board_rank="3")


def test_free_text_is_sanitized_on_the_way_in() -> None:
    t = task(1, summary="<img src=x>Ship​ it")

    assert t.summary == "Ship it"


def test_blockers_sorted_and_deduplicated() -> None:
    t = task(1, blocked_by=(IssueId(9), IssueId(3), IssueId(9)))

    assert t.blocked_by == (3, 9)


def test_snapshot_hash_ignores_fetch_time_and_input_order() -> None:
    a, b = task(2, "ABC-2"), task(1, "ABC-1")
    first = snapshot((a, b), fetched_at=T0)
    second = snapshot((b, a), fetched_at=T0 + timedelta(minutes=5))

    assert [t.issue_id for t in first.tasks] == [1, 2]
    assert first.snapshot_hash == second.snapshot_hash


def test_snapshot_hash_changes_with_content() -> None:
    before = snapshot((task(1),))
    after = snapshot((task(1, status_category=StatusCategory.DONE),))

    assert before.snapshot_hash != after.snapshot_hash


def test_moved_issue_keeps_identity() -> None:
    """DESIGN §18.1 #20: a new key with the same ID is the same issue."""
    before = snapshot((task(7, "OLD-1"),))
    after = snapshot((task(7, "NEW-9"),))

    assert before.tasks[0].issue_id == after.tasks[0].issue_id
    assert before.tasks[0].issue_key != after.tasks[0].issue_key


def test_duplicate_issue_ids_rejected() -> None:
    with pytest.raises(ValidationError, match="Duplicate issue_id"):
        snapshot((task(1, "A-1"), task(1, "B-1")))


def test_snapshot_json_round_trip() -> None:
    transition = StatusTransition(
        issue_id=IssueId(1), entry_id=100, from_status_id="1", to_status_id="3", at=T0
    )
    original = snapshot((task(1),), transitions=(transition,))

    restored = BoardSnapshot.model_validate_json(original.model_dump_json())

    assert restored == original
    assert restored.snapshot_hash == original.snapshot_hash


def forecast(**changes: object) -> Forecast:
    fields: dict[str, object] = {
        "method": ForecastMethod.MONTE_CARLO,
        "p50": date(2026, 11, 1),
        "p85": date(2026, 11, 8),
        "p95": date(2026, 11, 15),
        "trial_count": 10_000,
        "seed": 1,
        "history_weeks": 12,
        "confidence": Confidence.MEDIUM,
        "confidence_reason": "40% of open issues estimated",
    }
    fields.update(changes)
    return Forecast.model_validate(fields)


def test_forecast_percentiles_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="P50 <= P85 <= P95"):
        forecast(p85=date(2026, 10, 1))


def test_plan_round_trip() -> None:
    scheduled = ScheduledTask(
        issue_id=IssueId(1),
        issue_key=IssueKey("ABC-1"),
        start=date(2026, 10, 1),
        end=date(2026, 10, 3),
        effort_hours=Hours(16.0),
        duration_working_days=WorkingDays(2.0),
        effort_source=EffortSource.STORY_POINTS,
        lane=AccountId("acc-1"),
        constraint_reason=ConstraintReason.PLAN_DATE,
        total_float_working_days=WorkingDays(0.0),
        critical=True,
    )
    plan = Plan(
        plan_id=PlanId("p-1"),
        site_id=SiteId("acme"),
        board_ids=(BoardId(42),),
        account_id=AccountId("acc-1"),
        plan_date=date(2026, 10, 1),
        team_time_zone="Europe/Berlin",
        engine_version="0.1.0",
        settings_hash="s",
        snapshot_hash="h",
        scheduled_tasks=(scheduled,),
        dependency_edges=(),
        critical_paths=((IssueId(1),),),
        forecast=forecast(),
        milestones=(),
        warnings=(),
        solver_status=SolverStatus.OPTIMAL,
        generated_at=T0,
    )

    assert plan.schema_version == PLAN_SCHEMA_VERSION
    assert Plan.model_validate_json(plan.model_dump_json()) == plan


def test_plan_rejects_unknown_time_zone() -> None:
    with pytest.raises(ValidationError, match="IANA"):
        Plan.model_validate_json('{"team_time_zone": "Nowhere/City"}')


def test_scheduled_task_cannot_end_before_start() -> None:
    with pytest.raises(ValidationError, match="end before"):
        ScheduledTask(
            issue_id=IssueId(1),
            issue_key=IssueKey("ABC-1"),
            start=date(2026, 10, 3),
            end=date(2026, 10, 1),
            effort_hours=Hours(1.0),
            duration_working_days=WorkingDays(1.0),
            effort_source=EffortSource.DEFAULT,
            lane=None,
            constraint_reason=ConstraintReason.PLAN_DATE,
            total_float_working_days=WorkingDays(0.0),
            critical=False,
        )


def test_export_must_expire_after_creation() -> None:
    with pytest.raises(ValidationError, match="expire after"):
        ExportRecord(
            export_id=ExportId("e-1"),
            site_id=SiteId("acme"),
            account_id=AccountId("acc-1"),
            report_id=ReportId.GANTT,
            format=ExportFormat.HTML,
            media_type="text/html",
            size_bytes=10,
            created=T0,
            expires=T0,
            resource_uri="pulseboard://exports/e-1",
        )
