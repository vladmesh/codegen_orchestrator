"""Retire the environment-slot temporary access lifecycle: every grant has a target

Revision ID: 4d8e1f2a3b5c
Revises: a7c3e9d1f5b2
Create Date: 2026-09-22 14:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "4d8e1f2a3b5c"
down_revision: str | None = "a7c3e9d1f5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "temporary_access_grants"


def upgrade() -> None:
    connection = op.get_bind()
    live = (
        connection.execute(
            sa.text(
                "SELECT id FROM temporary_access_grants "
                "WHERE (target_base_url IS NULL OR target_application_id IS NULL) "
                "AND status != 'revoked' ORDER BY id"
            )
        )
        .scalars()
        .all()
    )
    if live:
        raise RuntimeError(
            f"Migration {revision} refuses to delete live target-less temporary access "
            f"grants; revoke or archive them first: {', '.join(live)}"
        )
    connection.execute(
        sa.text(
            "DELETE FROM temporary_access_grants "
            "WHERE target_base_url IS NULL OR target_application_id IS NULL"
        )
    )
    op.alter_column(TABLE, "target_application_id", existing_type=sa.Integer(), nullable=False)
    op.alter_column(TABLE, "target_base_url", existing_type=sa.String(length=2048), nullable=False)
    op.drop_column(TABLE, "env_key")
    op.drop_column(TABLE, "subject")


def downgrade() -> None:
    op.add_column(TABLE, sa.Column("subject", sa.String(length=255), nullable=True))
    op.add_column(TABLE, sa.Column("env_key", sa.String(length=255), nullable=True))
    op.alter_column(TABLE, "target_base_url", existing_type=sa.String(length=2048), nullable=True)
    op.alter_column(TABLE, "target_application_id", existing_type=sa.Integer(), nullable=True)
