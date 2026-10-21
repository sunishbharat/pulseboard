"""Initial schema: snapshots, board state, plans and exports.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

from pulseboard.store.schema import UtcDateTime

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the initial tables."""
    op.create_table(
        "snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("site_id", sa.String(40), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("board_id", sa.Integer, nullable=False),
        sa.Column("fetched_at", UtcDateTime, nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("full_reload", sa.Boolean, nullable=False),
        sa.Column(
            "baseline_key",
            sa.String(64),
            nullable=True,
            comment="Set on baselines, e.g. sprint:11 or week:2026-W43",
        ),
        sa.Column("data", sa.Text, nullable=False),
    )
    op.create_index(
        "ix_snapshots_board", "snapshots", ["site_id", "account_id", "board_id", "fetched_at"]
    )
    op.create_table(
        "board_state",
        sa.Column("site_id", sa.String(40), primary_key=True),
        sa.Column("account_id", sa.String(128), primary_key=True),
        sa.Column("board_id", sa.Integer, primary_key=True),
        sa.Column("last_fetch_at", UtcDateTime, nullable=False),
        sa.Column("refresh_count", sa.Integer, nullable=False),
        sa.Column("latest_snapshot_id", sa.Integer, nullable=False),
    )
    op.create_table(
        "plans",
        sa.Column("plan_id", sa.String(64), primary_key=True),
        sa.Column("site_id", sa.String(40), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("board_key", sa.String(256), nullable=False, comment="Comma-separated board IDs"),
        sa.Column("settings_hash", sa.String(64), nullable=False),
        sa.Column("plan_date", sa.String(10), nullable=False),
        sa.Column("generated_at", UtcDateTime, nullable=False),
        sa.Column("data", sa.Text, nullable=False),
    )
    op.create_index(
        "ix_plans_lookup",
        "plans",
        ["site_id", "account_id", "board_key", "settings_hash", "generated_at"],
    )
    op.create_table(
        "exports",
        sa.Column("export_id", sa.String(64), primary_key=True),
        sa.Column("site_id", sa.String(40), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("created", UtcDateTime, nullable=False),
        sa.Column("expires", UtcDateTime, nullable=False),
        sa.Column(
            "path", sa.String(512), nullable=False, comment="Relative to the export directory"
        ),
        sa.Column("data", sa.Text, nullable=False),
    )
    op.create_index("ix_exports_owner", "exports", ["site_id", "account_id"])


def downgrade() -> None:
    """Drop every table."""
    op.drop_index("ix_exports_owner", "exports")
    op.drop_table("exports")
    op.drop_index("ix_plans_lookup", "plans")
    op.drop_table("plans")
    op.drop_table("board_state")
    op.drop_index("ix_snapshots_board", "snapshots")
    op.drop_table("snapshots")
