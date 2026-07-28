"""Allow platform-level incidents without a server

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-07-28 09:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "b5c6d7e8f9a0"
down_revision: str | None = "a4b5c6d7e8f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CHECK_NAME = "ck_incidents_server_handle_required"
CHECK_SQL = "server_handle IS NOT NULL OR incident_type = 'provider_api_unavailable'"


def upgrade() -> None:
    op.alter_column(
        "incidents",
        "server_handle",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    # Only a provider outage belongs to no server. Every other type is deduplicated
    # by (server_handle, incident_type), and NULLs never collide in that index.
    op.create_check_constraint(CHECK_NAME, "incidents", CHECK_SQL)


def downgrade() -> None:
    op.drop_constraint(CHECK_NAME, "incidents", type_="check")
    op.execute("DELETE FROM incidents WHERE server_handle IS NULL")
    op.alter_column(
        "incidents",
        "server_handle",
        existing_type=sa.String(length=255),
        nullable=False,
    )
