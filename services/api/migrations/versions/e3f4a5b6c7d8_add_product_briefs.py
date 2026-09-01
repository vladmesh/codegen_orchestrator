"""Add immutable Product Brief and requirement coverage storage.

Revision ID: e3f4a5b6c7d8
Revises: d1e2f3a4b5c6
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "e3f4a5b6c7d8"
down_revision: str | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


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
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["story_id"], ["stories.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "revision", name="uq_product_brief_revision"),
        sa.UniqueConstraint("request_id"),
        sa.UniqueConstraint("story_id"),
        sa.UniqueConstraint("confirmation_request_id"),
    )
    op.create_index("ix_product_briefs_project_id", "product_briefs", ["project_id"])
    op.create_index("ix_product_briefs_story_id", "product_briefs", ["story_id"])
    op.create_table(
        "requirement_coverages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("brief_id", sa.String(length=64), nullable=False),
        sa.Column("requirement_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=255), nullable=True),
        sa.Column("repository_acceptance_contract", sa.Text(), nullable=True),
        sa.Column("returned_reason", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["brief_id"], ["product_briefs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("brief_id", "requirement_id", name="uq_requirement_coverage"),
    )
    op.create_index("ix_requirement_coverages_brief_id", "requirement_coverages", ["brief_id"])
    op.add_column("stories", sa.Column("product_brief_id", sa.String(length=64), nullable=True))
    op.create_unique_constraint("uq_stories_product_brief_id", "stories", ["product_brief_id"])


def downgrade() -> None:
    op.drop_constraint("uq_stories_product_brief_id", "stories", type_="unique")
    op.drop_column("stories", "product_brief_id")
    op.drop_index("ix_requirement_coverages_brief_id", table_name="requirement_coverages")
    op.drop_table("requirement_coverages")
    op.drop_index("ix_product_briefs_story_id", table_name="product_briefs")
    op.drop_index("ix_product_briefs_project_id", table_name="product_briefs")
    op.drop_table("product_briefs")
