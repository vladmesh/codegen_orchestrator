"""Create repositories table and add Task.repository_id

Revision ID: a154d8c67e28
Revises: f2a3b4c5d6e7
Create Date: 2026-03-08 02:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a154d8c67e28"
down_revision: str | None = "f2a3b4c5d6e7"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "repositories",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(255),
            sa.ForeignKey("projects.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("git_url", sa.String(512), nullable=False),
        sa.Column("provider_repo_id", sa.Integer(), nullable=True),
        sa.Column("role", sa.String(50), nullable=False, server_default="primary"),
        sa.Column("is_managed", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.add_column(
        "tasks",
        sa.Column(
            "repository_id",
            sa.String(255),
            sa.ForeignKey("repositories.id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("tasks", "repository_id")
    op.drop_table("repositories")
