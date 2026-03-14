"""Add application_id FK and rename status to result on service_deployments.

Revision ID: 9364785e242f
Revises: ff88df957f52
Create Date: 2026-03-14 15:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "9364785e242f"
down_revision: str | None = "ff88df957f52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add application_id FK (nullable for existing data)
    op.add_column(
        "service_deployments",
        sa.Column("application_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_service_deployments_application_id",
        "service_deployments",
        "applications",
        ["application_id"],
        ["id"],
    )
    op.create_index(
        op.f("ix_service_deployments_application_id"),
        "service_deployments",
        ["application_id"],
        unique=False,
    )

    # Rename status → result
    op.alter_column(
        "service_deployments",
        "status",
        new_column_name="result",
    )


def downgrade() -> None:
    # Rename result → status
    op.alter_column(
        "service_deployments",
        "result",
        new_column_name="status",
    )

    # Drop application_id
    op.drop_index(
        op.f("ix_service_deployments_application_id"),
        table_name="service_deployments",
    )
    op.drop_constraint(
        "fk_service_deployments_application_id",
        "service_deployments",
        type_="foreignkey",
    )
    op.drop_column("service_deployments", "application_id")
