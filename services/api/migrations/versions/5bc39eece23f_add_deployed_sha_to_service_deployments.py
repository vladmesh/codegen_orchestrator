"""add deployed_sha to service_deployments

Revision ID: 5bc39eece23f
Revises: 001_add_tasks
Create Date: 2026-02-16 03:34:34.310571

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "5bc39eece23f"
down_revision: str | None = "001_add_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("service_deployments", sa.Column("deployed_sha", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("service_deployments", "deployed_sha")
