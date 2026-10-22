"""Snapshot store: migrations, round trips, retention (DESIGN §11.2, §11.6, ADR-010)."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine

from factories import snapshot, task
from pulseboard.domain.plan import ExportRecord, Forecast, Plan
from pulseboard.domain.types import (
    AccountId,
    BoardId,
    Confidence,
    ExportFormat,
    ExportId,
    ForecastMethod,
    PlanId,
    ReportId,
    SiteId,
    SolverStatus,
)
from pulseboard.settings import Retention
from pulseboard.store.repository import (
    BoardState,
    SnapshotStore,
    board_key,
    create_store_engine,
    migrate,
)
from pulseboard.store.schema import metadata

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
SITE, ACCOUNT, BOARD = SiteId("acme"), AccountId("acc-1"), BoardId(42)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    engine = create_store_engine(f"sqlite:///{tmp_path / 'store.db'}")
    migrate(engine)
    return engine


@pytest.fixture
def store(engine: Engine) -> SnapshotStore:
    return SnapshotStore(engine)


def plan(
    plan_id: str, generated_at: datetime, settings_hash: str = "s", snapshot_hash: str = "h"
) -> Plan:
    end = date(2026, 11, 1)
    return Plan(
        plan_id=PlanId(plan_id),
        site_id=SITE,
        board_ids=(BOARD,),
        account_id=ACCOUNT,
        plan_date=generated_at.date(),
        team_time_zone="UTC",
        engine_version="0.1.0",
        settings_hash=settings_hash,
        snapshot_hash=snapshot_hash,
        scheduled_tasks=(),
        dependency_edges=(),
        critical_paths=(),
        forecast=Forecast(
            method=ForecastMethod.SCHEDULE,
            p50=end,
            p85=end,
            p95=end,
            confidence=Confidence.HIGH,
            confidence_reason="x",
        ),
        milestones=(),
        warnings=(),
        solver_status=SolverStatus.OPTIMAL,
        generated_at=generated_at,
    )


def test_migrations_match_the_schema(engine: Engine) -> None:
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn), metadata)

    assert diffs == []


def test_migrate_is_idempotent(engine: Engine) -> None:
    migrate(engine)  # already at head


def test_snapshot_round_trip(store: SnapshotStore) -> None:
    original = snapshot((task(1), task(2)))
    snapshot_id = store.save_snapshot(original, full_reload=True)
    stored = store.get_snapshot(snapshot_id)

    assert stored is not None
    assert stored.snapshot == original
    assert stored.snapshot.snapshot_hash == original.snapshot_hash
    assert stored.full_reload
    assert store.get_snapshot(999) is None


def test_baselines(store: SnapshotStore) -> None:
    store.save_snapshot(snapshot((task(1),)), full_reload=True, baseline_key="sprint:11")
    store.save_snapshot(snapshot((task(1),)), full_reload=True)

    assert store.has_baseline(SITE, ACCOUNT, BOARD, "sprint:11")
    assert not store.has_baseline(SITE, ACCOUNT, BOARD, "sprint:12")
    assert [b.baseline_key for b in store.baselines(SITE, ACCOUNT, BOARD)] == ["sprint:11"]


def test_board_state_upsert(store: SnapshotStore) -> None:
    assert store.board_state(SITE, ACCOUNT, BOARD) is None
    store.set_board_state(SITE, ACCOUNT, BOARD, BoardState(NOW, 1, 7))
    store.set_board_state(SITE, ACCOUNT, BOARD, BoardState(NOW + timedelta(minutes=3), 2, 8))

    assert store.board_state(SITE, ACCOUNT, BOARD) == BoardState(NOW + timedelta(minutes=3), 2, 8)


def test_plans(store: SnapshotStore) -> None:
    store.save_plan(plan("p1", NOW - timedelta(hours=2)))
    store.save_plan(plan("p2", NOW - timedelta(hours=1)))
    store.save_plan(plan("p3", NOW, settings_hash="other"))
    store.save_plan(plan("p1", NOW))  # immutable: ignored

    stored = store.get_plan(PlanId("p1"))
    assert stored is not None
    assert stored.generated_at == NOW - timedelta(hours=2)
    assert store.get_plan(PlanId("unknown")) is None
    latest = store.latest_plan(SITE, ACCOUNT, (BOARD,), "s")
    assert latest is not None
    assert latest.plan_id == "p2"
    assert store.latest_plan(SITE, AccountId("someone-else"), (BOARD,), "s") is None


def test_board_key_is_order_independent() -> None:
    assert board_key([BoardId(3), BoardId(1)]) == board_key([BoardId(1), BoardId(3)]) == "1,3"


def export(export_id: str, created: datetime, days: int = 7) -> ExportRecord:
    return ExportRecord(
        export_id=ExportId(export_id),
        site_id=SITE,
        account_id=ACCOUNT,
        report_id=ReportId.GANTT,
        format=ExportFormat.HTML,
        media_type="text/html",
        size_bytes=10,
        created=created,
        expires=created + timedelta(days=days),
        resource_uri=f"pulseboard://exports/{export_id}",
    )


def test_exports(store: SnapshotStore) -> None:
    store.save_export(export("e1", NOW), "acme/acc-1/e1.html")
    stored = store.get_export(ExportId("e1"))

    assert stored is not None
    assert stored.path == "acme/acc-1/e1.html"
    assert stored.record.report_id is ReportId.GANTT
    assert store.get_export(ExportId("nope")) is None


def test_purge_respects_retention(store: SnapshotStore) -> None:
    retention = Retention()
    old = snapshot((task(1),)).model_copy(update={"fetched_at": NOW - timedelta(days=400)})
    old_latest = snapshot((task(2),)).model_copy(update={"fetched_at": NOW - timedelta(days=400)})
    store.save_snapshot(old, full_reload=True)
    kept_id = store.save_snapshot(old_latest, full_reload=True)
    store.set_board_state(SITE, ACCOUNT, BOARD, BoardState(NOW, 1, kept_id))
    store.save_snapshot(snapshot((task(3),)), full_reload=True)
    store.save_plan(plan("old", NOW - timedelta(days=31)))
    store.save_plan(plan("new", NOW - timedelta(days=1)))
    store.save_export(export("expired", NOW - timedelta(days=8)), "acme/acc-1/expired.html")
    store.save_export(export("live", NOW - timedelta(days=1)), "acme/acc-1/live.html")

    counts = store.purge(NOW, retention)

    assert counts.snapshots == 1  # the board's latest snapshot is kept even when old
    assert counts.plans == 1
    assert counts.exports == ("acme/acc-1/expired.html",)
    assert store.get_snapshot(kept_id) is not None
    assert store.get_plan(PlanId("new")) is not None
    assert store.get_export(ExportId("live")) is not None


def test_purge_account(store: SnapshotStore) -> None:
    store.save_snapshot(snapshot((task(1),)), full_reload=True)
    store.save_plan(plan("p", NOW))
    store.save_export(export("e", NOW), "acme/acc-1/e.html")
    store.set_board_state(SITE, ACCOUNT, BOARD, BoardState(NOW, 1, 1))

    counts = store.purge_account(SITE, ACCOUNT)

    assert (counts.snapshots, counts.plans, counts.exports) == (1, 1, ("acme/acc-1/e.html",))
    assert store.board_state(SITE, ACCOUNT, BOARD) is None


def test_naive_datetimes_are_rejected(store: SnapshotStore) -> None:
    from sqlalchemy.exc import StatementError

    from pulseboard.store.repository import BoardState as State

    with pytest.raises(StatementError, match="naive"):
        store.set_board_state(SITE, ACCOUNT, BOARD, State(datetime(2026, 1, 1), 1, 1))  # noqa: DTZ001 - the point of the test


def test_in_memory_engine_keeps_data() -> None:
    engine = create_store_engine("sqlite://")
    migrate(engine)
    store = SnapshotStore(engine)
    snapshot_id = store.save_snapshot(snapshot((task(1),)), full_reload=True)

    assert store.get_snapshot(snapshot_id) is not None


def test_purge_keeps_snapshots_that_kept_plans_use(store: SnapshotStore) -> None:
    """PB-04: an old snapshot stays while a plan within retention was built from it."""
    used = snapshot((task(1),)).model_copy(update={"fetched_at": NOW - timedelta(days=400)})
    unused = snapshot((task(2),)).model_copy(update={"fetched_at": NOW - timedelta(days=400)})
    used_id = store.save_snapshot(used, full_reload=True)
    unused_id = store.save_snapshot(unused, full_reload=True)
    store.save_plan(plan("recent", NOW - timedelta(days=1), snapshot_hash=used.snapshot_hash))
    store.save_plan(plan("old", NOW - timedelta(days=31), snapshot_hash=unused.snapshot_hash))

    counts = store.purge(NOW, Retention())

    assert counts.plans == 1
    assert counts.snapshots == 1
    assert store.get_snapshot(used_id) is not None
    assert store.get_snapshot(unused_id) is None  # its only plan was purged first
    assert store.plan_uses_snapshot(SITE, ACCOUNT, BOARD, used.snapshot_hash)
    assert not store.plan_uses_snapshot(SITE, ACCOUNT, BOARD, unused.snapshot_hash)


def test_migration_0002_records_existing_plans_snapshot_hash(tmp_path: Path) -> None:
    """Plans stored before 0002 get their snapshot hash from their own data."""
    from alembic import command
    from alembic.config import Config

    from pulseboard.store.repository import MIGRATIONS

    engine = create_store_engine(f"sqlite:///{tmp_path / 'old.db'}")
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0001")
    old = plan("before", NOW, snapshot_hash="abc")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO plans (plan_id, site_id, account_id, board_key, settings_hash, "
            "plan_date, generated_at, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "before",
                SITE,
                ACCOUNT,
                "42",
                "s",
                "2026-10-19",
                NOW.isoformat(),
                old.model_dump_json(),
            ),
        )

    migrate(engine)

    assert SnapshotStore(engine).plan_uses_snapshot(SITE, ACCOUNT, BOARD, "abc")
