"""Add temporary access grants

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-07-28 13:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "c6d7e8f9a0b1"
down_revision: str | None = "b5c6d7e8f9a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LIVE_SLOT_INDEX = "uq_temporary_access_grants_live_slot"


def upgrade() -> None:
    op.create_table(
        "temporary_access_grants",
        sa.Column("id", sa.String(length=255), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("env_key", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column("qa_run_id", sa.String(length=255), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="granted"),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.String(length=50), nullable=True),
        sa.Column("revoke_run_id", sa.String(length=255), nullable=True),
        sa.Column("revoke_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_temporary_access_grants_project_id", "temporary_access_grants", ["project_id"]
    )
    op.create_index(
        "ix_temporary_access_grants_qa_run_id", "temporary_access_grants", ["qa_run_id"]
    )
    op.create_index("ix_temporary_access_grants_status", "temporary_access_grants", ["status"])
    op.create_index(
        "ix_temporary_access_grants_granted_at", "temporary_access_grants", ["granted_at"]
    )
    # The contract holds one value per key, so two live grants for the same slot
    # would overwrite each other and one of them could never be revoked.
    op.create_index(
        LIVE_SLOT_INDEX,
        "temporary_access_grants",
        ["project_id", "env_key"],
        unique=True,
        postgresql_where=sa.text("status != 'revoked'"),
    )


def downgrade() -> None:
    op.drop_index(LIVE_SLOT_INDEX, table_name="temporary_access_grants")
    op.drop_index("ix_temporary_access_grants_granted_at", table_name="temporary_access_grants")
    op.drop_index("ix_temporary_access_grants_status", table_name="temporary_access_grants")
    op.drop_index("ix_temporary_access_grants_qa_run_id", table_name="temporary_access_grants")
    op.drop_index("ix_temporary_access_grants_project_id", table_name="temporary_access_grants")
    op.drop_table("temporary_access_grants")
