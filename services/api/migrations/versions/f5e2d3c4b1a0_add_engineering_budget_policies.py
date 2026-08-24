"""Add durable per-user engineering-budget policies.

Revision ID: f5e2d3c4b1a0
Revises: 8d2c5e6f7a8b
Create Date: 2026-08-24 17:20:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f5e2d3c4b1a0"
down_revision: str | None = "8d2c5e6f7a8b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_state = postgresql.ENUM(
    "enabled", "disabled", name="engineering_budget_policy_state", create_type=False
)


def upgrade() -> None:
    _state.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "engineering_budget_policies",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("limit_microusd", sa.BigInteger(), nullable=False),
        sa.Column("state", _state, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("limit_microusd >= 0", name="ck_engineering_budget_policy_limit"),
        sa.CheckConstraint("version >= 1", name="ck_engineering_budget_policy_version"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("engineering_budget_policies")
    _state.drop(op.get_bind(), checkfirst=True)
