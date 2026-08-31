"""Add durable grant intents independent from deploy runs.

Revision ID: a8c1d2e3f4a5
Revises: f4a5b6c7d8e9
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "a8c1d2e3f4a5"
down_revision: str | None = "f4a5b6c7d8e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users_grant_intents",
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("target_application_id", sa.Integer(), nullable=True),
        sa.Column("target_deployment_id", sa.Integer(), nullable=True),
        sa.Column("target_sha", sa.String(length=64), nullable=False),
        sa.Column("target_history", sa.JSON(), nullable=False),
        sa.Column("initiating_actor", sa.String(length=255), nullable=False),
        sa.Column("outgoing_owner_id", sa.Integer(), nullable=True),
        sa.Column("incoming_owner_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("execution_run_id", sa.String(length=255), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["execution_run_id"], ["runs.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "kind", "project_id", "channel", "external_id", name="uq_users_grant_intent"
        ),
    )
    op.create_index(op.f("ix_users_grant_intents_kind"), "users_grant_intents", ["kind"])
    op.create_index(
        op.f("ix_users_grant_intents_project_id"), "users_grant_intents", ["project_id"]
    )
    op.create_index(op.f("ix_users_grant_intents_status"), "users_grant_intents", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_users_grant_intents_status"), table_name="users_grant_intents")
    op.drop_index(op.f("ix_users_grant_intents_project_id"), table_name="users_grant_intents")
    op.drop_index(op.f("ix_users_grant_intents_kind"), table_name="users_grant_intents")
    op.drop_table("users_grant_intents")
