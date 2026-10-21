"""Build plans from test snapshots with the real engine."""

from datetime import UTC, datetime

from factories import PLAN_DATE
from pulseboard.domain.plan import Plan
from pulseboard.domain.snapshot import BoardSnapshot
from pulseboard.domain.types import PlanId
from pulseboard.engine.planner import PlanRequest, build_plan
from pulseboard.engine.pyjobshop_scheduler import PyJobShopScheduler
from pulseboard.engine.warnings import load_rules
from pulseboard.settings import Settings

SETTINGS = Settings()


def plan_of(board: BoardSnapshot, plan_id: str = "a1b2c3d4e5f6") -> Plan:
    request = PlanRequest(
        snapshots=(board,),
        settings=SETTINGS,
        plan_date=PLAN_DATE,
        plan_id=PlanId(plan_id),
        engine_version="0.1.0",
        settings_hash="s",
        generated_at=datetime(2026, 10, 19, 7, tzinfo=UTC),
        seed=int(plan_id[:8], 16),
    )
    plan, _ = build_plan(request, PyJobShopScheduler(), load_rules())
    return plan
