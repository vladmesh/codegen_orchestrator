"""Persist a FEATURE's product-local package build intent beside its ownership."""

from alembic import op
import sqlalchemy as sa

revision = "c7e2a9f4d6b1"
down_revision = "adafaa34bf3d"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("package_build", sa.JSON(), nullable=True))
    op.create_check_constraint(
        "ck_tasks_package_build",
        "tasks",
        "package_build IS NULL OR (type = 'feature' AND install IS NULL "
        "AND story_id IS NOT NULL AND repository_id IS NOT NULL)",
    )


def downgrade():
    op.drop_constraint("ck_tasks_package_build", "tasks", type_="check")
    op.drop_column("tasks", "package_build")
