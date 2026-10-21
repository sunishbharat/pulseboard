"""Typed access to the snapshot store (DESIGN §11.2, §11.6, ADR-009, ADR-010)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, and_, create_engine, delete, desc, insert, select, update
from sqlalchemy.pool import StaticPool

from pulseboard.domain.plan import ExportRecord, Plan
from pulseboard.domain.snapshot import BoardSnapshot
from pulseboard.domain.types import AccountId, BoardId, ExportId, PlanId, SiteId
from pulseboard.settings import Retention
from pulseboard.store.schema import board_state, exports, plans, snapshots

MIGRATIONS = Path(__file__).parent / "migrations"


def create_store_engine(url: str) -> Engine:
    """An engine for ``url``; in-memory SQLite shares one connection so data persists."""
    if url in ("sqlite://", "sqlite:///:memory:"):
        return create_engine(url, poolclass=StaticPool, connect_args={"check_same_thread": False})
    return create_engine(url)


def migrate(engine: Engine) -> None:
    """Upgrade the database to the latest schema (``alembic upgrade head``)."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


@dataclass(frozen=True)
class BoardState:
    """Refresh bookkeeping for one board (DESIGN §9.5)."""

    last_fetch_at: datetime
    refresh_count: int
    latest_snapshot_id: int


@dataclass(frozen=True)
class StoredSnapshot:
    """A snapshot with its row ID and baseline marker."""

    snapshot_id: int
    snapshot: BoardSnapshot
    baseline_key: str | None
    full_reload: bool


@dataclass(frozen=True)
class StoredExport:
    """An export record and its file path relative to the export directory."""

    record: ExportRecord
    path: str


@dataclass(frozen=True)
class PurgeCounts:
    """Rows removed by a purge."""

    snapshots: int
    plans: int
    exports: tuple[str, ...]
    """Relative paths of export files to delete."""


def board_key(board_ids: Sequence[BoardId]) -> str:
    """The plan lookup key for a set of boards."""
    return ",".join(str(b) for b in sorted(board_ids))


class SnapshotStore:
    """Snapshots, baselines, plans and export records, keyed by (site, account)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ------------------------------------------------------------ snapshots

    def save_snapshot(
        self, snapshot: BoardSnapshot, *, full_reload: bool, baseline_key: str | None = None
    ) -> int:
        """Store a snapshot and return its ID."""
        with self._engine.begin() as conn:
            result = conn.execute(
                insert(snapshots).values(
                    site_id=snapshot.site_id,
                    account_id=snapshot.account_id,
                    board_id=snapshot.board_id,
                    fetched_at=snapshot.fetched_at,
                    snapshot_hash=snapshot.snapshot_hash,
                    full_reload=full_reload,
                    baseline_key=baseline_key,
                    data=snapshot.model_dump_json(),
                )
            )
            key = result.inserted_primary_key
            return int(key[0]) if key else 0

    def get_snapshot(self, snapshot_id: int) -> StoredSnapshot | None:
        """A stored snapshot by ID."""
        with self._engine.connect() as conn:
            row = conn.execute(select(snapshots).where(snapshots.c.id == snapshot_id)).first()
        if row is None:
            return None
        return StoredSnapshot(
            row.id, BoardSnapshot.model_validate_json(row.data), row.baseline_key, row.full_reload
        )

    def snapshot_by_hash(
        self, site: SiteId, account: AccountId, board: BoardId, snapshot_hash: str
    ) -> BoardSnapshot | None:
        """The newest stored snapshot of a board with this content hash."""
        with self._engine.connect() as conn:
            row = conn.execute(
                select(snapshots.c.data)
                .where(
                    snapshots.c.site_id == site,
                    snapshots.c.account_id == account,
                    snapshots.c.board_id == board,
                    snapshots.c.snapshot_hash == snapshot_hash,
                )
                .order_by(desc(snapshots.c.id))
            ).first()
        return None if row is None else BoardSnapshot.model_validate_json(row.data)

    def known_account(self, site: SiteId, board: BoardId) -> AccountId | None:
        """The account that last refreshed a board (local mode: the only user)."""
        with self._engine.connect() as conn:
            row = conn.execute(
                select(board_state.c.account_id)
                .where(board_state.c.site_id == site, board_state.c.board_id == board)
                .order_by(desc(board_state.c.last_fetch_at))
            ).first()
        return None if row is None else AccountId(row.account_id)

    def delete_snapshot(self, snapshot_id: int) -> None:
        """Remove a superseded incremental snapshot."""
        with self._engine.begin() as conn:
            conn.execute(delete(snapshots).where(snapshots.c.id == snapshot_id))

    def has_baseline(self, site: SiteId, account: AccountId, board: BoardId, key: str) -> bool:
        """Whether a baseline with ``key`` exists for the board."""
        with self._engine.connect() as conn:
            row = conn.execute(
                select(snapshots.c.id).where(
                    snapshots.c.site_id == site,
                    snapshots.c.account_id == account,
                    snapshots.c.board_id == board,
                    snapshots.c.baseline_key == key,
                )
            ).first()
        return row is not None

    def baselines(self, site: SiteId, account: AccountId, board: BoardId) -> list[StoredSnapshot]:
        """All baselines of a board, newest first."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(snapshots)
                .where(
                    snapshots.c.site_id == site,
                    snapshots.c.account_id == account,
                    snapshots.c.board_id == board,
                    snapshots.c.baseline_key.is_not(None),
                )
                .order_by(desc(snapshots.c.fetched_at), desc(snapshots.c.id))
            ).all()
        return [
            StoredSnapshot(
                r.id, BoardSnapshot.model_validate_json(r.data), r.baseline_key, r.full_reload
            )
            for r in rows
        ]

    # ------------------------------------------------------------ refresh state

    def board_state(self, site: SiteId, account: AccountId, board: BoardId) -> BoardState | None:
        """Refresh bookkeeping for a board, if it was fetched before."""
        with self._engine.connect() as conn:
            row = conn.execute(
                select(board_state).where(
                    board_state.c.site_id == site,
                    board_state.c.account_id == account,
                    board_state.c.board_id == board,
                )
            ).first()
        if row is None:
            return None
        return BoardState(row.last_fetch_at, row.refresh_count, row.latest_snapshot_id)

    def set_board_state(
        self, site: SiteId, account: AccountId, board: BoardId, state: BoardState
    ) -> None:
        """Insert or replace a board's refresh bookkeeping."""
        values = {
            "last_fetch_at": state.last_fetch_at,
            "refresh_count": state.refresh_count,
            "latest_snapshot_id": state.latest_snapshot_id,
        }
        key = and_(
            board_state.c.site_id == site,
            board_state.c.account_id == account,
            board_state.c.board_id == board,
        )
        with self._engine.begin() as conn:
            if conn.execute(update(board_state).where(key).values(**values)).rowcount == 0:
                conn.execute(
                    insert(board_state).values(
                        site_id=site, account_id=account, board_id=board, **values
                    )
                )

    # ------------------------------------------------------------ plans

    def save_plan(self, plan: Plan) -> None:
        """Store an immutable plan (idempotent: an existing plan ID is kept)."""
        if self.get_plan(plan.plan_id) is not None:
            return
        with self._engine.begin() as conn:
            conn.execute(
                insert(plans).values(
                    plan_id=plan.plan_id,
                    site_id=plan.site_id,
                    account_id=plan.account_id,
                    board_key=board_key(plan.board_ids),
                    settings_hash=plan.settings_hash,
                    plan_date=plan.plan_date.isoformat(),
                    generated_at=plan.generated_at,
                    data=plan.model_dump_json(),
                )
            )

    def get_plan(self, plan_id: PlanId) -> Plan | None:
        """A plan by ID, or ``None`` if unknown or purged."""
        with self._engine.connect() as conn:
            row = conn.execute(select(plans.c.data).where(plans.c.plan_id == plan_id)).first()
        return None if row is None else Plan.model_validate_json(row.data)

    def latest_plan(
        self, site: SiteId, account: AccountId, boards: Sequence[BoardId], settings_hash: str
    ) -> Plan | None:
        """The newest plan for the same site, account, boards and settings (stale-plan policy)."""
        with self._engine.connect() as conn:
            row = conn.execute(
                select(plans.c.data)
                .where(
                    plans.c.site_id == site,
                    plans.c.account_id == account,
                    plans.c.board_key == board_key(boards),
                    plans.c.settings_hash == settings_hash,
                )
                .order_by(desc(plans.c.generated_at))
            ).first()
        return None if row is None else Plan.model_validate_json(row.data)

    # ------------------------------------------------------------ exports

    def save_export(self, record: ExportRecord, path: str) -> None:
        """Store an export record."""
        with self._engine.begin() as conn:
            conn.execute(
                insert(exports).values(
                    export_id=record.export_id,
                    site_id=record.site_id,
                    account_id=record.account_id,
                    created=record.created,
                    expires=record.expires,
                    path=path,
                    data=record.model_dump_json(),
                )
            )

    def get_export(self, export_id: ExportId) -> StoredExport | None:
        """An export record by ID."""
        with self._engine.connect() as conn:
            row = conn.execute(select(exports).where(exports.c.export_id == export_id)).first()
        if row is None:
            return None
        return StoredExport(ExportRecord.model_validate_json(row.data), row.path)

    # ------------------------------------------------------------ retention

    def purge(self, now: datetime, retention: Retention) -> PurgeCounts:
        """Delete rows past their retention (ADR-010). Snapshots referenced as a board's latest
        are kept. Returns the export files the caller should delete."""
        snapshot_cutoff = now - timedelta(days=retention.snapshots_days)
        plan_cutoff = now - timedelta(days=retention.plans_days)
        with self._engine.begin() as conn:
            latest = select(board_state.c.latest_snapshot_id)
            removed_snapshots = conn.execute(
                delete(snapshots).where(
                    snapshots.c.fetched_at < snapshot_cutoff, snapshots.c.id.not_in(latest)
                )
            ).rowcount
            removed_plans = conn.execute(
                delete(plans).where(plans.c.generated_at < plan_cutoff)
            ).rowcount
            expired = (
                conn.execute(select(exports.c.path).where(exports.c.expires <= now)).scalars().all()
            )
            conn.execute(delete(exports).where(exports.c.expires <= now))
        return PurgeCounts(removed_snapshots, removed_plans, tuple(expired))

    def purge_account(self, site: SiteId, account: AccountId) -> PurgeCounts:
        """Delete everything stored for one (site, account), on request (ADR-010)."""
        with self._engine.begin() as conn:
            files = (
                conn.execute(
                    select(exports.c.path).where(
                        exports.c.site_id == site, exports.c.account_id == account
                    )
                )
                .scalars()
                .all()
            )
            counts = []
            for table in (snapshots, plans, exports, board_state):
                counts.append(
                    conn.execute(
                        delete(table).where(table.c.site_id == site, table.c.account_id == account)
                    ).rowcount
                )
        return PurgeCounts(counts[0], counts[1], tuple(files))
