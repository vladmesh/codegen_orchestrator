"""add product briefs, requirement coverage and the task dispatch admission

Revision ID: b7c1e4a90d23
Revises: c3f7a91d2b48
Create Date: 2026-09-02 06:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "b7c1e4a90d23"
down_revision: str | None = "c3f7a91d2b48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_briefs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("story_id", sa.String(length=255), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("request_id", sa.String(length=255), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmation_request_id", sa.String(length=255), nullable=True),
        sa.Column("coverage_admitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("planning_attempt_id", sa.String(length=128), nullable=True),
        sa.Column(
            "planning_attempt_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("planning_attempt_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["story_id"], ["stories.id"]),
        sa.PrimaryKeyConstraint("id"),
        # One brief backs at most one story, and one story is backed by at most
        # one brief, so "the tasks of this brief" is a well-defined roster.
        sa.UniqueConstraint("story_id"),
        sa.UniqueConstraint("request_id"),
        sa.UniqueConstraint("confirmation_request_id"),
        sa.UniqueConstraint("project_id", "revision", name="uq_product_brief_revision"),
    )
    op.create_index(
        op.f("ix_product_briefs_project_id"), "product_briefs", ["project_id"], unique=False
    )
    op.create_index(
        op.f("ix_product_briefs_story_id"), "product_briefs", ["story_id"], unique=False
    )

    op.create_table(
        "requirement_coverages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("brief_id", sa.String(length=64), nullable=False),
        sa.Column("requirement_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=255), nullable=True),
        sa.Column("returned_reason", sa.Text(), nullable=True),
        sa.Column("planning_attempt_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["brief_id"], ["product_briefs.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("brief_id", "requirement_id", name="uq_requirement_coverage"),
    )
    op.create_index(
        op.f("ix_requirement_coverages_brief_id"),
        "requirement_coverages",
        ["brief_id"],
        unique=False,
    )

    # True for every row that exists: a task written before the
    # coverage-to-dispatch boundary existed was never planned against a brief,
    # so it keeps dispatching exactly as it does today. `server_default` is what
    # backfills them — one statement, no window in which the column is unknown —
    # and it stays on the column so the same is true of every non-brief task
    # created later.
    op.add_column(
        "tasks",
        sa.Column(
            "dispatch_admitted", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
    )
    op.add_column("tasks", sa.Column("planning_attempt_id", sa.String(length=128), nullable=True))
    op.create_index(
        op.f("ix_tasks_planning_attempt_id"), "tasks", ["planning_attempt_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_tasks_planning_attempt_id"), table_name="tasks")
    op.drop_column("tasks", "planning_attempt_id")
    op.drop_column("tasks", "dispatch_admitted")
    op.drop_index(op.f("ix_requirement_coverages_brief_id"), table_name="requirement_coverages")
    op.drop_table("requirement_coverages")
    op.drop_index(op.f("ix_product_briefs_story_id"), table_name="product_briefs")
    op.drop_index(op.f("ix_product_briefs_project_id"), table_name="product_briefs")
    op.drop_table("product_briefs")
