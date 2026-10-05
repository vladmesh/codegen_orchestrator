"""create_job_commands

Revision ID: 9c2e5a71d4f3
Revises: 4d8c1f9a2b7e
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "9c2e5a71d4f3"
down_revision = "4d8c1f9a2b7e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_commands",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("command_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("fired_by_product", sa.String(length=255), nullable=False),
        sa.Column("fired_by_run", sa.String(length=255), nullable=False),
        sa.Column(
            "dispatch_status",
            sa.Enum("dispatched", "undelivered", name="job_dispatch_status"),
            nullable=False,
        ),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "fired_by_product", "command_id", name="uq_job_commands_product_command"
        ),
    )


def downgrade() -> None:
    op.drop_table("job_commands")
    op.execute("DROP TYPE IF EXISTS job_dispatch_status")
