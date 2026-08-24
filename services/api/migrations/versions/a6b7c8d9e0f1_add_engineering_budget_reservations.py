"""Add durable engineering budget reservations.

Revision ID: a6b7c8d9e0f1
Revises: f5e2d3c4b1a0
Create Date: 2026-08-24 18:55:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a6b7c8d9e0f1"
down_revision: str | None = "f5e2d3c4b1a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_policy_state = postgresql.ENUM(
    "enabled", "disabled", name="engineering_budget_policy_state", create_type=False
)
_reservation_state = postgresql.ENUM(
    "active",
    "released",
    "unknown_final",
    "settled",
    name="engineering_budget_reservation_state",
    create_type=False,
)
_admission_outcome = postgresql.ENUM(
    "admitted",
    "denied",
    "unlimited",
    "not_enforced",
    name="engineering_budget_admission_outcome",
    create_type=False,
)


def upgrade() -> None:
    _reservation_state.create(op.get_bind(), checkfirst=True)
    _admission_outcome.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "engineering_budget_policies",
        sa.Column(
            "attempt_reservation_microusd", sa.BigInteger(), nullable=False, server_default="0"
        ),
    )
    op.create_check_constraint(
        "ck_engineering_budget_policy_attempt_reservation",
        "engineering_budget_policies",
        "attempt_reservation_microusd >= 0",
    )
    op.alter_column(
        "engineering_budget_policies", "attempt_reservation_microusd", server_default=None
    )
    op.create_table(
        "engineering_budget_reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=320), nullable=False),
        sa.Column("attempt_id", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("story_id", sa.String(length=255), nullable=True),
        sa.Column("task_id", sa.String(length=255), nullable=True),
        sa.Column("outcome", _admission_outcome, nullable=False),
        sa.Column("state", _reservation_state, nullable=True),
        sa.Column("reservation_microusd", sa.BigInteger(), nullable=False),
        sa.Column("known_spend_microusd", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("active_held_microusd", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "reservation_microusd >= 0", name="ck_engineering_budget_reservation_amount"
        ),
        sa.CheckConstraint(
            "known_spend_microusd >= 0", name="ck_engineering_budget_reservation_known_spend"
        ),
        sa.CheckConstraint(
            "active_held_microusd >= 0", name="ck_engineering_budget_reservation_active_held"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("attempt_id"),
    )
    op.create_index(
        "ix_engineering_budget_reservation_user_id", "engineering_budget_reservations", ["user_id"]
    )
    op.create_index(
        "ix_engineering_budget_reservation_user_state",
        "engineering_budget_reservations",
        ["user_id", "state"],
    )
    op.create_index(
        "ix_engineering_budget_reservation_project_id",
        "engineering_budget_reservations",
        ["project_id"],
    )
    op.create_index(
        "ix_engineering_budget_reservation_story_id",
        "engineering_budget_reservations",
        ["story_id"],
    )
    op.create_index(
        "ix_engineering_budget_reservation_task_id", "engineering_budget_reservations", ["task_id"]
    )


def downgrade() -> None:
    op.drop_table("engineering_budget_reservations")
    op.drop_constraint(
        "ck_engineering_budget_policy_attempt_reservation", "engineering_budget_policies"
    )
    op.drop_column("engineering_budget_policies", "attempt_reservation_microusd")
    _admission_outcome.drop(op.get_bind(), checkfirst=True)
    _reservation_state.drop(op.get_bind(), checkfirst=True)
