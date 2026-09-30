"""Add the per-project QA probe library.

Revision ID: 7a1c3e5d9b2f
Revises: 6f2a9c4e8b1d
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "7a1c3e5d9b2f"
down_revision: str | None = "6f2a9c4e8b1d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "qa_probes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("file_kind", sa.String(length=8), nullable=False),
        sa.Column("origin_run_id", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("platform IN ('telegram', 'http', 'web')", name="ck_qa_probes_platform"),
        sa.CheckConstraint("file_kind IN ('py', 'sh')", name="ck_qa_probes_file_kind"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "platform", "name", name="uq_qa_probes_project_name"),
    )
    op.create_index("ix_qa_probes_project_updated", "qa_probes", ["project_id", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_qa_probes_project_updated", table_name="qa_probes")
    op.drop_table("qa_probes")
