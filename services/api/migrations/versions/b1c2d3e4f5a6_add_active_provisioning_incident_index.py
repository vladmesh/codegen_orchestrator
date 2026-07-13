"""Enforce one active provisioning incident per server.

Revision ID: b1c2d3e4f5a6
Revises: a9c4e8f1b2d3
Create Date: 2026-07-13 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "a9c4e8f1b2d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX uq_incidents_active_provisioning_failure "
        "ON incidents (server_handle, incident_type) "
        "WHERE incident_type = 'provisioning_failed' "
        "AND status IN ('detected', 'recovering')"
    )


def downgrade() -> None:
    op.drop_index("uq_incidents_active_provisioning_failure", table_name="incidents")
