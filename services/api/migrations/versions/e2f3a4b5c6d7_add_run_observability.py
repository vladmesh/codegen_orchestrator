"""add run observability fields

Revision ID: e2f3a4b5c6d7
Revises: d8e9f0a1b2c3
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "e2f3a4b5c6d7"
down_revision: str | None = "d8e9f0a1b2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("input_tokens", sa.Integer(), nullable=True))
    op.add_column("runs", sa.Column("output_tokens", sa.Integer(), nullable=True))
    op.add_column("runs", sa.Column("total_tokens", sa.Integer(), nullable=True))
    op.add_column("runs", sa.Column("cost_usd", sa.Float(), nullable=True))
    op.add_column("runs", sa.Column("agent_profile", sa.JSON(), nullable=True))
    op.add_column("runs", sa.Column("transcript_path", sa.String(length=1024), nullable=True))
    op.add_column("runs", sa.Column("transcript_truncated", sa.Boolean(), nullable=True))
    op.create_index(op.f("ix_runs_started_at"), "runs", ["started_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_runs_started_at"), table_name="runs")
    op.drop_column("runs", "transcript_truncated")
    op.drop_column("runs", "transcript_path")
    op.drop_column("runs", "agent_profile")
    op.drop_column("runs", "cost_usd")
    op.drop_column("runs", "total_tokens")
    op.drop_column("runs", "output_tokens")
    op.drop_column("runs", "input_tokens")
