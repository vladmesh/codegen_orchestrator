"""add story unverified decisions

Revision ID: 9c3e5a7b1d2f
Revises: 8b2d4f6a0c3e
Create Date: 2026-09-26 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "9c3e5a7b1d2f"
down_revision: str | None = "8b2d4f6a0c3e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "stories",
        sa.Column(
            "unverified_decisions",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("stories", "unverified_decisions")
