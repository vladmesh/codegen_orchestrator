"""Separate operator stop and publication recovery from paid attempt outcomes.

API human-review owns stories.engineering_stop. API recovery owns the one
claim/receipt per engineering attempt; worker-manager reads the API identity
and supplies exact native Git proof. Paid Runs and accounting remain unchanged.
"""

from alembic import op
import sqlalchemy as sa

revision = "e5a7c9d1f3b6"
down_revision = "d7a1c5e9b3f4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("stories", sa.Column("engineering_stop", sa.JSON(), nullable=True))
    op.create_table(
        "commit_recoveries",
        sa.Column(
            "attempt_id",
            sa.String(255),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "story_id",
            sa.String(255),
            sa.ForeignKey("stories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("stop_id", sa.String(255), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("identity", sa.JSON(), nullable=False),
        sa.Column("receipt", sa.JSON(), nullable=True),
        sa.Column("handed_off_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade():
    op.drop_table("commit_recoveries")
    op.drop_column("stories", "engineering_stop")
