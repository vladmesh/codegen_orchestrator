"""add unique constraint on port_allocations server_handle port

Revision ID: be730dff04d5
Revises: a1b2c3d4e5f6
Create Date: 2026-03-05 21:15:46.427312

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "be730dff04d5"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint("uq_server_port", "port_allocations", ["server_handle", "port"])


def downgrade() -> None:
    op.drop_constraint("uq_server_port", "port_allocations", type_="unique")
