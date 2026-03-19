"""split_project_status_add_service_status

Add service_status to projects, status to repositories.
Data-migrate old ProjectStatus values to new lifecycle + service_status split.

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-03-12 10:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b3c4d5e6f7a8"
down_revision: str | None = "a2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- Add new columns ---
    op.add_column(
        "projects",
        sa.Column(
            "service_status", sa.String(length=50), nullable=False, server_default="not_deployed"
        ),
    )
    op.add_column(
        "repositories",
        sa.Column("status", sa.String(length=50), nullable=False, server_default="active"),
    )

    # --- Data migration: map old status values to new lifecycle + service_status ---
    conn = op.get_bind()

    # active → active, service_status=running
    conn.execute(sa.text("UPDATE projects SET service_status = 'running' WHERE status = 'active'"))
    # deploying → active, service_status=running (was mid-deploy)
    conn.execute(
        sa.text(
            "UPDATE projects SET status = 'active', service_status = 'running' "
            "WHERE status = 'deploying'"
        )
    )
    # developing/testing → active, service_status=running (was mid-work on active project)
    conn.execute(
        sa.text(
            "UPDATE projects SET status = 'active', service_status = 'running' "
            "WHERE status IN ('developing', 'testing')"
        )
    )
    # scaffolding/scaffolded/scaffold_failed → draft, not_deployed
    conn.execute(
        sa.text(
            "UPDATE projects SET status = 'draft' "
            "WHERE status IN ('scaffolding', 'scaffolded', 'scaffold_failed')"
        )
    )
    # failed/error → active, service_status=down
    conn.execute(
        sa.text(
            "UPDATE projects SET status = 'active', service_status = 'down' "
            "WHERE status IN ('failed', 'error')"
        )
    )
    # missing → active, not_deployed + mark repo as missing
    conn.execute(
        sa.text(
            "UPDATE repositories SET status = 'missing' "
            "WHERE project_id IN (SELECT id FROM projects WHERE status = 'missing')"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE projects SET status = 'active', service_status = 'not_deployed' "
            "WHERE status = 'missing'"
        )
    )
    # maintenance → active, service_status=stopped
    conn.execute(
        sa.text(
            "UPDATE projects SET status = 'active', service_status = 'stopped' "
            "WHERE status = 'maintenance'"
        )
    )
    # archived → archived, service_status=stopped
    conn.execute(
        sa.text("UPDATE projects SET service_status = 'stopped' WHERE status = 'archived'")
    )
    # draft stays draft, not_deployed (default already correct)

    # Remove server_default after data migration
    op.alter_column("projects", "service_status", server_default=None)
    op.alter_column("repositories", "status", server_default=None)


def downgrade() -> None:
    op.drop_column("repositories", "status")
    op.drop_column("projects", "service_status")
