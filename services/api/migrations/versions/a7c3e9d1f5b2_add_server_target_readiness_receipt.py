"""Record a managed target's key fingerprint, readiness receipt and readiness park

Revision ID: a7c3e9d1f5b2
Revises: d9e4f2a1b6c3
Create Date: 2026-09-13 22:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "a7c3e9d1f5b2"
down_revision: str | None = "d9e4f2a1b6c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "servers"


def upgrade() -> None:
    # Existing rows get no receipt: admission refuses them until the first
    # reconciliation proves the current QA target profile on each one.
    op.add_column(TABLE, sa.Column("ssh_key_fingerprint", sa.String(length=100), nullable=True))
    op.add_column(TABLE, sa.Column("qa_target_version", sa.String(length=64), nullable=True))
    op.add_column(TABLE, sa.Column("qa_target_proved_at", sa.DateTime(), nullable=True))
    op.add_column(
        TABLE, sa.Column("target_readiness_failure_phase", sa.String(length=50), nullable=True)
    )
    op.add_column(
        TABLE, sa.Column("target_readiness_parked_status", sa.String(length=50), nullable=True)
    )
    # One active readiness incident per server, beside and never inside the
    # provisioning-failure episode.
    op.execute(
        "CREATE UNIQUE INDEX uq_incidents_active_target_not_ready "
        "ON incidents (server_handle, incident_type) "
        "WHERE incident_type = 'target_not_ready' "
        "AND status IN ('detected', 'recovering')"
    )


def downgrade() -> None:
    op.drop_index("uq_incidents_active_target_not_ready", table_name="incidents")
    op.drop_column(TABLE, "target_readiness_parked_status")
    op.drop_column(TABLE, "target_readiness_failure_phase")
    op.drop_column(TABLE, "qa_target_proved_at")
    op.drop_column(TABLE, "qa_target_version")
    op.drop_column(TABLE, "ssh_key_fingerprint")
