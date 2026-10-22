"""Plan building end to end in the engine, with a golden plan (DESIGN §10, §18 golden row)."""

from datetime import UTC, datetime

from syrupy.assertion import SnapshotAssertion

from factories import PLAN_DATE, fixture_board, snapshot, task
from pulseboard.domain.plan import Plan
from pulseboard.domain.types import Confidence, ForecastMethod, IssueId, PlanId, WarningId
from pulseboard.engine.planner import PlanRequest, build_plan, combined_snapshot_hash
from pulseboard.engine.pyjobshop_scheduler import PyJobShopScheduler
from pulseboard.engine.warnings import load_rules
from pulseboard.settings import PointsMode, PointsPolicy, Settings, TeamSettings

GENERATED = datetime(2026, 10, 19, 7, 0, tzinfo=UTC)


def plan_for(*snapshots: object, settings: Settings | None = None) -> Plan:
    request = PlanRequest(
        snapshots=snapshots,  # type: ignore[arg-type]  # snapshots built by factories
        settings=settings or Settings(),
        plan_date=PLAN_DATE,
        plan_id=PlanId("plan-1"),
        engine_version="0.1.0",
        settings_hash="settings-hash",
        generated_at=GENERATED,
        seed=1,
    )
    plan, _ = build_plan(request, PyJobShopScheduler(), load_rules())
    return plan


def test_golden_fixture_plan(snapshot: SnapshotAssertion) -> None:
    plan = plan_for(fixture_board())

    assert plan.model_dump_json(indent=1) == snapshot
    assert Plan.model_validate_json(plan.model_dump_json()) == plan


def test_plan_is_byte_identical_across_runs() -> None:
    assert (
        plan_for(fixture_board()).model_dump_json() == plan_for(fixture_board()).model_dump_json()
    )


def test_fixture_plan_contents() -> None:
    plan = plan_for(fixture_board())
    rule_ids = {w.rule_id for w in plan.warnings}

    assert plan.forecast.method is ForecastMethod.SCHEDULE
    assert len(plan.scheduled_tasks) == 9
    assert WarningId.MISSING_ESTIMATE in rule_ids  # ABC-108
    assert WarningId.UNASSIGNED in rule_ids  # ABC-111
    assert WarningId.MILESTONE_RISK in rule_ids
    assert WarningId.DUE_DATE_MISS in rule_ids  # ABC-109
    assert plan.critical_paths[0][-1] == 112


def test_low_estimated_share_lowers_confidence() -> None:
    plan = plan_for(snapshot((task(1), task(2), task(3, story_points=1.0))))

    assert plan.forecast.confidence is Confidence.LOW
    assert plan.estimated_share < 0.6
    assert WarningId.LOW_CONFIDENCE in {w.rule_id for w in plan.warnings}


def test_fully_estimated_board_is_high_confidence() -> None:
    fixed = Settings(
        team=TeamSettings(points=PointsPolicy(mode=PointsMode.FIXED, fixed_points_per_day=1))
    )
    plan = plan_for(snapshot((task(1, story_points=1.0),)), settings=fixed)

    assert plan.forecast.confidence is Confidence.HIGH
    assert WarningId.POINTS_RATE_FALLBACK not in {w.rule_id for w in plan.warnings}


def test_empty_board_plan() -> None:
    plan = plan_for(snapshot(()))

    assert plan.scheduled_tasks == ()
    assert plan.forecast.p50 == PLAN_DATE


def test_disabled_warnings_are_skipped() -> None:
    settings = Settings(disabled_warnings=(WarningId.UNASSIGNED,))
    plan = plan_for(snapshot((task(1, assignee=None),)), settings=settings)

    assert WarningId.UNASSIGNED not in {w.rule_id for w in plan.warnings}


def test_combined_hash_for_several_boards() -> None:
    a, b = snapshot((task(1),)), snapshot((task(2),))

    assert combined_snapshot_hash([a]) == a.snapshot_hash
    assert combined_snapshot_hash([a, b]) == combined_snapshot_hash([b, a])
    assert plan_for(a, b, settings=Settings(team=TeamSettings())).board_ids == (42, 42)


def closed_sprint_board(closed_sprints: int) -> object:
    """Two story-pointed open tasks; ``closed_sprints`` closed sprints with completed points."""
    from pulseboard.domain.snapshot import Sprint
    from pulseboard.domain.types import SprintId, SprintState, StatusCategory
    from pulseboard.domain.untrusted import UntrustedText

    sprints = tuple(
        Sprint(
            sprint_id=SprintId(s),
            name=UntrustedText(f"S{s}"),
            state=SprintState.CLOSED,
            start=datetime(2026, 9, 7 + 14 * s, 9, tzinfo=UTC),
            end=datetime(2026, 9, 18 + 14 * s, 17, tzinfo=UTC),
        )
        for s in range(closed_sprints)
    )
    done = tuple(
        task(10 + s, category=StatusCategory.DONE, sprint_id=SprintId(s), story_points=20.0)
        for s in range(closed_sprints)
    )
    return snapshot((task(1, story_points=5.0), task(2, story_points=3.0), *done), sprints=sprints)


def test_the_points_fallback_rate_is_announced() -> None:
    """PB-19: without a closed sprint, points convert at the fallback rate: W017 and lower
    confidence. One closed sprint with completed points gives a velocity: neither."""
    fallback = plan_for(closed_sprint_board(0))
    velocity = plan_for(closed_sprint_board(1))
    fallback_rules = {w.rule_id for w in fallback.warnings}

    assert WarningId.POINTS_RATE_FALLBACK in fallback_rules
    assert "fallback rate of 1 points per day" in fallback.forecast.confidence_reason
    assert fallback.forecast.confidence is Confidence.MEDIUM  # HIGH, one step down
    assert WarningId.POINTS_RATE_FALLBACK not in {w.rule_id for w in velocity.warnings}
    assert velocity.forecast.confidence is Confidence.HIGH


def test_a_configured_link_type_missing_from_the_board_is_reported() -> None:
    """W015 (PB-06): a configured type that matches no link on the board adds nothing."""
    board = snapshot((task(1), task(2, blocked_by=(IssueId(1),))))
    other = Settings(
        team=TeamSettings.model_validate(
            {"dependency_link_types": ["Blocks", {"name": "Depends", "blocker": "outward"}]}
        )
    )

    found = [w for w in plan_for(board, settings=other).warnings]
    unknown = [w for w in found if w.rule_id is WarningId.UNKNOWN_LINK_TYPE]

    assert len(unknown) == 1
    assert "'Depends'" in unknown[0].message
    assert "link types on the board: Blocks" in unknown[0].message
    assert WarningId.UNKNOWN_LINK_TYPE not in {w.rule_id for w in plan_for(board).warnings}


def test_a_people_setting_that_matches_no_one_is_reported() -> None:
    """W016 (PB-16)."""
    team = TeamSettings.model_validate({"people": [{"account_id": "nobody", "capacity": 0.5}]})

    found = plan_for(snapshot((task(1),)), settings=Settings(team=team)).warnings

    assert [w.message.split("'")[1] for w in found if w.rule_id is WarningId.UNKNOWN_PERSON] == [
        "nobody"
    ]
