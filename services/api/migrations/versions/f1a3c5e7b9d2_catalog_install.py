"""Persist typed catalog installs and their non-engineering settlement."""

from alembic import op
import sqlalchemy as sa

revision = "f1a3c5e7b9d2"
down_revision = "e5a7c9d1f3b6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("install", sa.JSON(), nullable=True))
    op.add_column("tasks", sa.Column("install_operation", sa.JSON(), nullable=True))
    op.create_check_constraint(
        "ck_tasks_install_payload",
        "tasks",
        "(type = 'install' AND install IS NOT NULL AND story_id IS NOT NULL "
        "AND repository_id IS NOT NULL) OR (type <> 'install' AND install IS NULL)",
    )


def downgrade():
    op.drop_constraint("ck_tasks_install_payload", "tasks", type_="check")
    op.drop_column("tasks", "install_operation")
    op.drop_column("tasks", "install")
