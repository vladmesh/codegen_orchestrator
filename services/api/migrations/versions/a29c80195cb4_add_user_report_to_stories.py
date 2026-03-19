"""add_user_report_to_stories

Revision ID: a29c80195cb4
Revises: b3c4d5e6f7a8
Create Date: 2026-03-12 14:41:28.520559

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a29c80195cb4"
down_revision: str | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("user_report", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("stories", "user_report")
