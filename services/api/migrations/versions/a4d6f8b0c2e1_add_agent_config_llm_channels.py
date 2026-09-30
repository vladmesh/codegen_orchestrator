"""add agent config llm channels

Revision ID: a4d6f8b0c2e1
Revises: 9c3e5a7b1d2f
Create Date: 2026-09-26 18:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "a4d6f8b0c2e1"
down_revision: str | None = "9c3e5a7b1d2f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NULL keeps every existing agent on the default chain: codex, claude, openrouter.
    op.add_column("agent_configs", sa.Column("llm_channels", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_configs", "llm_channels")
