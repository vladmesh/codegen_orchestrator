"""remove service_status from projects

Revision ID: 6cca2fda6c83
Revises: 64deccf2a991
Create Date: 2026-03-14 21:17:41.534882

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "6cca2fda6c83"
down_revision: str | None = "64deccf2a991"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("projects", "service_status")


def downgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "service_status",
            sa.VARCHAR(length=50),
            server_default="not_deployed",
            nullable=False,
        ),
    )
