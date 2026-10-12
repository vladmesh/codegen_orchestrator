"""Pull requests linked to a story by an operator or automation.

A new table only: no existing row changes meaning. A link belongs to its story and
is removed with it.
"""

from alembic import op
import sqlalchemy as sa

revision = "b8d4f2a6c1e3"
down_revision = "a7c3e9f1b5d8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "story_links",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "story_id",
            sa.String(255),
            sa.ForeignKey("stories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_story_links_story_id", "story_links", ["story_id"])


def downgrade():
    op.drop_index("ix_story_links_story_id", table_name="story_links")
    op.drop_table("story_links")
