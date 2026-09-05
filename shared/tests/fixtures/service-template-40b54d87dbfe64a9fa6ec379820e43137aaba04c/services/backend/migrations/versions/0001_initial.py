"""empty initial migration"""

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401

from services.backend.src.core.orm import Base

metadata = Base.metadata

# revision identifiers, used by Alembic.
revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Run upgrade migrations."""
    # This migration intentionally left blank; serves as starting point referencing metadata.
    pass


def downgrade() -> None:
    """Run downgrade migrations."""
    pass
