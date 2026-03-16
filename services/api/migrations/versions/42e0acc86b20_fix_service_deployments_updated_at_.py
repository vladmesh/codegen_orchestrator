"""fix_service_deployments_updated_at_default

Revision ID: 42e0acc86b20
Revises: 4e96ae47d969
Create Date: 2026-03-16 11:35:09.583370

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "42e0acc86b20"
down_revision: str | None = "4e96ae47d969"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "service_deployments",
        "updated_at",
        server_default=sa.text("now()"),
    )


def downgrade() -> None:
    op.alter_column(
        "service_deployments",
        "updated_at",
        server_default=None,
    )
