"""Record the product-local kit packages live stories built, one row per product package."""

from alembic import op
import sqlalchemy as sa

revision = "e4b8c1d7a9f2"
down_revision = "c7e2a9f4d6b1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "package_candidates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("story_id", sa.String(length=255), nullable=False),
        sa.Column("repository_id", sa.String(length=255), nullable=False),
        sa.Column("source_task_id", sa.String(length=255), nullable=False),
        sa.Column("install_task_id", sa.String(length=255), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("package_name", sa.String(length=64), nullable=False),
        sa.Column("package_distribution", sa.String(length=128), nullable=False),
        sa.Column("package_version", sa.String(length=255), nullable=False),
        sa.Column("commit", sa.String(length=40), nullable=False),
        sa.Column("core_version", sa.String(length=255), nullable=False),
        sa.Column("python_version", sa.String(length=255), nullable=False),
        sa.Column("tooling_commit", sa.String(length=40), nullable=False),
        sa.Column("capability", sa.String(length=500), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["story_id"], ["stories.id"]),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"]),
        sa.ForeignKeyConstraint(["source_task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["install_task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "package_name", name="uq_package_candidates_project_package"
        ),
    )


def downgrade():
    op.drop_table("package_candidates")
