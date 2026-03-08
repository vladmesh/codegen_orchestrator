"""project_id_to_uuid_schema_cleanup

Revision ID: f4e731766f8c
Revises: 800ab7e1637d
Create Date: 2026-03-08 03:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "f4e731766f8c"
down_revision: str | None = "800ab7e1637d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# All tables with FK to projects.id
# FK names are the actual constraint names in the database
FK_TABLES = [
    ("tasks", "project_id", True, "work_items_project_id_fkey"),
    ("stories", "project_id", True, "stories_project_id_fkey"),
    ("brainstorms", "project_id", True, "brainstorms_project_id_fkey"),
    ("milestones", "project_id", True, "milestones_project_id_fkey"),
    ("repositories", "project_id", True, "repositories_project_id_fkey"),
    ("runs", "project_id", False, "fk_tasks_project_id"),
    ("port_allocations", "project_id", False, "port_allocations_project_id_fkey"),
    ("rag_documents", "project_id", False, "rag_documents_project_id_fkey"),
    ("rag_chunks", "project_id", False, "rag_chunks_project_id_fkey"),
    (
        "rag_conversation_summaries",
        "project_id",
        False,
        "rag_conversation_summaries_project_id_fkey",
    ),
    ("rag_messages", "project_id", False, "rag_messages_project_id_fkey"),
]

# Tables with project_id but no FK constraint
PLAIN_TABLES = [
    ("service_deployments", "project_id"),
    ("api_keys", "project_id"),
]


def upgrade() -> None:
    # 1. Drop all FK constraints referencing projects.id
    for table, _col, _, fk_name in FK_TABLES:
        op.drop_constraint(fk_name, table, type_="foreignkey")

    # 2. Convert projects.id from VARCHAR to UUID
    op.execute("ALTER TABLE projects ALTER COLUMN id TYPE uuid USING id::uuid")

    # 3. Convert all FK columns from VARCHAR to UUID
    for table, col, _not_null, _ in FK_TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {col} TYPE uuid USING {col}::uuid")

    # 4. Convert plain columns (no FK)
    for table, col in PLAIN_TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {col} TYPE uuid USING {col}::uuid")

    # 5. Re-create FK constraints
    for table, col, _, fk_name in FK_TABLES:
        op.create_foreign_key(fk_name, table, "projects", [col], ["id"])

    # 6. Drop legacy columns from projects
    op.drop_column("projects", "github_repo_id")
    op.drop_column("projects", "repository_url")

    # 7. Add visibility column to repositories
    op.add_column(
        "repositories",
        sa.Column("visibility", sa.String(20), server_default="private", nullable=False),
    )


def downgrade() -> None:
    # Remove visibility
    op.drop_column("repositories", "visibility")

    # Re-add legacy columns
    op.add_column(
        "projects",
        sa.Column("repository_url", sa.String(512), nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column("github_repo_id", sa.Integer(), nullable=True),
    )

    # Drop FK constraints
    for table, _col, _, fk_name in FK_TABLES:
        op.drop_constraint(fk_name, table, type_="foreignkey")

    # Convert back to VARCHAR
    for table, col in PLAIN_TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {col} TYPE varchar(255) USING {col}::text")

    for table, col, _, _ in FK_TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {col} TYPE varchar(255) USING {col}::text")

    op.execute("ALTER TABLE projects ALTER COLUMN id TYPE varchar(255) USING id::text")

    # Re-create FK constraints
    for table, col, _, fk_name in FK_TABLES:
        op.create_foreign_key(fk_name, table, "projects", [col], ["id"])
