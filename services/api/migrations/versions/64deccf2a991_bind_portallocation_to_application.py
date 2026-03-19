"""bind_portallocation_to_application

Move PortAllocation ownership from Project to Application.
- Add application_id FK to port_allocations
- Backfill application_id from existing data (match via server_handle)
- Drop project_id from port_allocations
- Drop port column from applications

Revision ID: 64deccf2a991
Revises: f1e492214fce
Create Date: 2026-03-14 16:24:33.107227

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "64deccf2a991"
down_revision: str | None = "f1e492214fce"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add application_id column to port_allocations (nullable for backfill)
    op.add_column("port_allocations", sa.Column("application_id", sa.Integer(), nullable=True))
    op.create_index(
        op.f("ix_port_allocations_application_id"),
        "port_allocations",
        ["application_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_port_allocations_application_id",
        "port_allocations",
        "applications",
        ["application_id"],
        ["id"],
    )

    # 2. Backfill: link existing port_allocations to applications via server_handle
    op.execute("""
        UPDATE port_allocations pa
        SET application_id = a.id
        FROM applications a
        WHERE pa.server_handle = a.server_handle
          AND pa.application_id IS NULL
    """)

    # 3. Drop project_id from port_allocations
    op.drop_constraint(
        op.f("port_allocations_project_id_fkey"),
        "port_allocations",
        type_="foreignkey",
    )
    op.drop_column("port_allocations", "project_id")

    # 4. Drop port from applications
    op.drop_column("applications", "port")


def downgrade() -> None:
    # 1. Restore port column on applications
    op.add_column(
        "applications",
        sa.Column("port", sa.INTEGER(), autoincrement=False, nullable=True),
    )

    # Backfill port from first port_allocation
    op.execute("""
        UPDATE applications a
        SET port = pa.port
        FROM port_allocations pa
        WHERE pa.application_id = a.id
          AND a.port IS NULL
    """)

    op.alter_column("applications", "port", nullable=False)

    # 2. Restore project_id on port_allocations
    op.add_column(
        "port_allocations",
        sa.Column("project_id", sa.UUID(), autoincrement=False, nullable=True),
    )
    op.create_foreign_key(
        op.f("port_allocations_project_id_fkey"),
        "port_allocations",
        "projects",
        ["project_id"],
        ["id"],
    )

    # 3. Drop application_id from port_allocations
    op.drop_constraint(
        "fk_port_allocations_application_id",
        "port_allocations",
        type_="foreignkey",
    )
    op.drop_index(
        op.f("ix_port_allocations_application_id"),
        table_name="port_allocations",
    )
    op.drop_column("port_allocations", "application_id")
