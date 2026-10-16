"""Store a user's creation and last activity as timezone-aware instants.

Every stored value was written as naive UTC, so it is read as UTC in both directions
and no instant shifts, whatever the session's TimeZone.
"""

from alembic import op
import sqlalchemy as sa

revision = "adafaa34bf3d"
down_revision = "b8d4f2a6c1e3"
branch_labels = None
depends_on = None

_COLUMNS = ("created_at", "last_seen")


def upgrade():
    for column in _COLUMNS:
        op.alter_column(
            "users",
            column,
            existing_type=sa.DateTime(),
            type_=sa.DateTime(timezone=True),
            existing_nullable=False,
            postgresql_using=f"{column} AT TIME ZONE 'UTC'",
        )


def downgrade():
    for column in _COLUMNS:
        op.alter_column(
            "users",
            column,
            existing_type=sa.DateTime(timezone=True),
            type_=sa.DateTime(),
            existing_nullable=False,
            postgresql_using=f"{column} AT TIME ZONE 'UTC'",
        )
