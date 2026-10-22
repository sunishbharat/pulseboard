"""Tables of the snapshot store. Changes here need an Alembic migration in ``migrations/``."""

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    Column,
    Dialect,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    TypeDecorator,
)


class UtcDateTime(TypeDecorator[datetime]):
    """An aware UTC datetime stored as ISO-8601 text (portable across SQLite and Postgres)."""

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> str | None:
        """Store as UTC ISO-8601; naive datetimes are rejected."""
        if value is None:
            return None
        if value.tzinfo is None:
            msg = "naive datetimes cannot be stored"
            raise ValueError(msg)
        return value.astimezone(UTC).isoformat()

    def process_result_value(self, value: str | None, dialect: Dialect) -> datetime | None:
        """Read back as an aware UTC datetime."""
        return None if value is None else datetime.fromisoformat(value)


metadata = MetaData()

snapshots = Table(
    "snapshots",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("site_id", String(40), nullable=False),
    Column("account_id", String(128), nullable=False),
    Column("board_id", Integer, nullable=False),
    Column("fetched_at", UtcDateTime, nullable=False),
    Column("snapshot_hash", String(64), nullable=False),
    Column("full_reload", Boolean, nullable=False),
    Column(
        "baseline_key",
        String(64),
        nullable=True,
        comment="Set on baselines, e.g. sprint:11 or week:2026-W43",
    ),
    Column("data", Text, nullable=False),
    Index("ix_snapshots_board", "site_id", "account_id", "board_id", "fetched_at"),
)

board_state = Table(
    "board_state",
    metadata,
    Column("site_id", String(40), primary_key=True),
    Column("account_id", String(128), primary_key=True),
    Column("board_id", Integer, primary_key=True),
    Column("last_fetch_at", UtcDateTime, nullable=False),
    Column("refresh_count", Integer, nullable=False),
    Column("latest_snapshot_id", Integer, nullable=False),
)

plans = Table(
    "plans",
    metadata,
    Column("plan_id", String(64), primary_key=True),
    Column("site_id", String(40), nullable=False),
    Column("account_id", String(128), nullable=False),
    Column("board_key", String(256), nullable=False, comment="Comma-separated board IDs"),
    Column("settings_hash", String(64), nullable=False),
    Column("plan_date", String(10), nullable=False),
    Column("generated_at", UtcDateTime, nullable=False),
    Column("data", Text, nullable=False),
    Column(
        "snapshot_hash", String(64), nullable=True, comment="The snapshot the plan was built from"
    ),
    Index("ix_plans_lookup", "site_id", "account_id", "board_key", "settings_hash", "generated_at"),
    Index("ix_plans_snapshot", "site_id", "account_id", "snapshot_hash"),
)

exports = Table(
    "exports",
    metadata,
    Column("export_id", String(64), primary_key=True),
    Column("site_id", String(40), nullable=False),
    Column("account_id", String(128), nullable=False),
    Column("created", UtcDateTime, nullable=False),
    Column("expires", UtcDateTime, nullable=False),
    Column("path", String(512), nullable=False, comment="Relative to the export directory"),
    Column("data", Text, nullable=False),
    Index("ix_exports_owner", "site_id", "account_id"),
)
