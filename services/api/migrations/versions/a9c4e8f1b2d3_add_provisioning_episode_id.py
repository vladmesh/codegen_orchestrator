"""add provisioning episode id

Revision ID: a9c4e8f1b2d3
Revises: 1e075b24917f
Create Date: 2026-07-13 13:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "a9c4e8f1b2d3"
down_revision: str | None = "1e075b24917f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "servers",
        sa.Column("provisioning_episode_id", sa.String(length=36), nullable=True),
    )
    op.execute("UPDATE servers SET provisioning_attempts = 0")


def downgrade() -> None:
    op.drop_column("servers", "provisioning_episode_id")
