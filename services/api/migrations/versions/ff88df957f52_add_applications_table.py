"""add_applications_table

Revision ID: ff88df957f52
Revises: a29c80195cb4
Create Date: 2026-03-14 14:54:27.957688

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "ff88df957f52"
down_revision: str | None = "a29c80195cb4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "applications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("repo_id", sa.String(length=255), nullable=False),
        sa.Column("server_handle", sa.String(length=255), nullable=False),
        sa.Column("service_name", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("last_health_check", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["repo_id"], ["repositories.id"]),
        sa.ForeignKeyConstraint(["server_handle"], ["servers.handle"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repo_id", "server_handle", name="uq_application_repo_server"),
    )
    op.create_index(op.f("ix_applications_repo_id"), "applications", ["repo_id"], unique=False)
    op.create_index(
        op.f("ix_applications_server_handle"),
        "applications",
        ["server_handle"],
        unique=False,
    )
    op.create_index(op.f("ix_applications_status"), "applications", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_applications_status"), table_name="applications")
    op.drop_index(op.f("ix_applications_server_handle"), table_name="applications")
    op.drop_index(op.f("ix_applications_repo_id"), table_name="applications")
    op.drop_table("applications")
