"""Bind temporary QA access to a generated-service capability target.

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "c0d1e2f3a4b5"
down_revision: str | None = "b9c0d1e2f3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "temporary_access_grants"
OLD_INDEX = "uq_temporary_access_grants_live_slot"
NEW_INDEX = "uq_temporary_access_grants_live_target"


def upgrade() -> None:
    op.drop_index(OLD_INDEX, table_name=TABLE)
    op.alter_column(TABLE, "env_key", existing_type=sa.String(length=255), nullable=True)
    op.alter_column(TABLE, "subject", existing_type=sa.String(length=255), nullable=True)
    op.add_column(TABLE, sa.Column("channel", sa.String(length=50), nullable=True))
    op.add_column(TABLE, sa.Column("external_id", sa.String(length=255), nullable=True))
    op.add_column(TABLE, sa.Column("target_application_id", sa.Integer(), nullable=True))
    op.add_column(TABLE, sa.Column("target_base_url", sa.String(length=2048), nullable=True))
    op.create_index(
        NEW_INDEX,
        TABLE,
        ["project_id", "target_application_id"],
        unique=True,
        postgresql_where=sa.text("status != 'revoked' AND target_application_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(NEW_INDEX, table_name=TABLE)
    op.drop_column(TABLE, "target_base_url")
    op.drop_column(TABLE, "target_application_id")
    op.drop_column(TABLE, "external_id")
    op.drop_column(TABLE, "channel")
    op.alter_column(TABLE, "subject", existing_type=sa.String(length=255), nullable=False)
    op.alter_column(TABLE, "env_key", existing_type=sa.String(length=255), nullable=False)
    op.create_index(
        OLD_INDEX,
        TABLE,
        ["project_id", "env_key"],
        unique=True,
        postgresql_where=sa.text("status != 'revoked'"),
    )
