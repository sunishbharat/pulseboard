"""Plan building end to end in the engine, with a golden plan (DESIGN §10, §18 golden row)."""

from datetime import UTC, datetime

from syrupy.assertion import SnapshotAssertion

from factories import PLAN_DATE, fixture_board, snapshot, task
from pulseboard.domain.plan import Plan
from pulseboard.domain.types import Confidence, ForecastMethod, PlanId, WarningId
from pulseboard.engine.planner import PlanRequest, build_plan, combined_snapshot_hash
from pulseboard.engine.pyjobshop_scheduler import PyJobShopScheduler
from pulseboard.engine.warnings import load_rules
from pulseboard.settings import Settings, TeamSettings

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
    plan = plan_for(snapshot((task(1, story_points=1.0),)))

    assert plan.forecast.confidence is Confidence.HIGH


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
