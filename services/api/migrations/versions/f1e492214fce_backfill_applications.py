"""Backfill applications from existing service_deployments data.

Creates Application records for each unique (project_id, server_handle, service_name)
and links existing deployments to them. Only backfills where a primary repository exists.
Sets all existing deployment results to 'success' (they were all successful deploys).

Revision ID: f1e492214fce
Revises: 9364785e242f
Create Date: 2026-03-14 15:10:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "f1e492214fce"
down_revision: str | None = "9364785e242f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()

    # Find unique deployable combos that have a matching primary repo
    rows = conn.execute(
        sa.text("""
            SELECT DISTINCT ON (sd.project_id, sd.server_handle, sd.service_name)
                sd.project_id,
                sd.server_handle,
                sd.service_name,
                sd.port,
                r.id AS repo_id
            FROM service_deployments sd
            JOIN repositories r
                ON r.project_id = sd.project_id
                AND r.role = 'primary'
            ORDER BY sd.project_id, sd.server_handle, sd.service_name,
                     sd.deployed_at DESC
        """)
    ).fetchall()

    for row in rows:
        # Insert Application
        result = conn.execute(
            sa.text("""
                INSERT INTO applications (repo_id, server_handle, service_name, port, status)
                VALUES (:repo_id, :server_handle, :service_name, :port, 'running')
                ON CONFLICT (repo_id, server_handle) DO NOTHING
                RETURNING id
            """),
            {
                "repo_id": row.repo_id,
                "server_handle": row.server_handle,
                "service_name": row.service_name,
                "port": row.port,
            },
        )
        app_row = result.fetchone()
        if app_row is None:
            # Already exists (shouldn't happen in practice)
            app_row = conn.execute(
                sa.text("""
                    SELECT id FROM applications
                    WHERE repo_id = :repo_id AND server_handle = :server_handle
                """),
                {"repo_id": row.repo_id, "server_handle": row.server_handle},
            ).fetchone()

        app_id = app_row.id

        # Link all matching deployments to this application
        conn.execute(
            sa.text("""
                UPDATE service_deployments
                SET application_id = :app_id
                WHERE project_id = :project_id
                  AND server_handle = :server_handle
                  AND service_name = :service_name
            """),
            {
                "app_id": app_id,
                "project_id": row.project_id,
                "server_handle": row.server_handle,
                "service_name": row.service_name,
            },
        )

    # Set all existing deployment results to 'success'
    # (they were all recorded after successful GH Actions runs)
    conn.execute(
        sa.text("""
            UPDATE service_deployments SET result = 'success' WHERE result = 'running'
        """)
    )


def downgrade() -> None:
    conn = op.get_bind()

    # Restore result → 'running'
    conn.execute(
        sa.text("""
            UPDATE service_deployments SET result = 'running' WHERE result = 'success'
        """)
    )

    # Unlink deployments
    conn.execute(sa.text("UPDATE service_deployments SET application_id = NULL"))

    # Delete all applications
    conn.execute(sa.text("DELETE FROM applications"))
