"""Plans record the snapshot hash they were built from, so a snapshot a plan uses is kept.

Revision ID: 0002
Revises: 0001
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add ``plans.snapshot_hash`` and fill it from each stored plan."""
    with op.batch_alter_table("plans") as batch:
        batch.add_column(sa.Column("snapshot_hash", sa.String(64), nullable=True))
    plans = sa.table(
        "plans",
        sa.column("plan_id", sa.String),
        sa.column("data", sa.Text),
        sa.column("snapshot_hash", sa.String),
    )
    connection = op.get_bind()
    for plan_id, data in connection.execute(sa.select(plans.c.plan_id, plans.c.data)).all():
        connection.execute(
            plans.update()
            .where(plans.c.plan_id == plan_id)
            .values(snapshot_hash=json.loads(data)["snapshot_hash"])
        )
    op.create_index("ix_plans_snapshot", "plans", ["site_id", "account_id", "snapshot_hash"])


def downgrade() -> None:
    """Drop ``plans.snapshot_hash``."""
    op.drop_index("ix_plans_snapshot", "plans")
    with op.batch_alter_table("plans") as batch:
        batch.drop_column("snapshot_hash")
