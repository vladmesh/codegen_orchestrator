"""create_event_consumptions

Revision ID: d4a7b2c9e1f0
Revises: 9c2e5a71d4f3
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "d4a7b2c9e1f0"
down_revision = "9c2e5a71d4f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_consumptions",
        sa.Column("consumer_group", sa.String(length=255), nullable=False),
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("consumer_group", "event_id"),
    )


def downgrade() -> None:
    op.drop_table("event_consumptions")
