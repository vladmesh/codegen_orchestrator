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

# All FK + plain tables for updating old_id → new_id
ALL_REF_TABLES = [t[0] for t in FK_TABLES] + [t[0] for t in PLAIN_TABLES]


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')

    # 1. Drop all FK constraints referencing projects.id
    for table, _col, _, fk_name in FK_TABLES:
        op.drop_constraint(fk_name, table, type_="foreignkey")

    # 2. Add a temporary new_id column to projects for ID mapping
    op.execute("ALTER TABLE projects ADD COLUMN new_id uuid")

    # 3. For rows that are already valid UUIDs, keep them; generate new ones for others
    # Valid UUID pattern: 8-4-4-4-12 hex with dashes (36 chars)
    op.execute("""
        UPDATE projects SET new_id = CASE
            WHEN id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN id::uuid
            ELSE uuid_generate_v4()
        END
    """)

    # 4. Update all referencing tables: replace old string IDs with new UUIDs
    for table in ALL_REF_TABLES:
        sql = (
            f"UPDATE {table} SET project_id = ("  # noqa: S608
            f" SELECT new_id::text FROM projects"
            f" WHERE projects.id = {table}.project_id"
            f") WHERE project_id IS NOT NULL"
        )
        op.execute(sql)

    # 5. Swap: set projects.id = new_id, then drop new_id
    op.execute("UPDATE projects SET id = new_id::text")
    op.execute("ALTER TABLE projects DROP COLUMN new_id")

    # 6. Now all IDs are valid UUIDs — convert column types
    op.execute("ALTER TABLE projects ALTER COLUMN id TYPE uuid USING id::uuid")

    for table, col, _not_null, _ in FK_TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {col} TYPE uuid USING {col}::uuid")

    for table, col in PLAIN_TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {col} TYPE uuid USING {col}::uuid")

    # 7. Re-create FK constraints
    for table, col, _, fk_name in FK_TABLES:
        op.create_foreign_key(fk_name, table, "projects", [col], ["id"])

    # 8. Drop legacy columns from projects
    op.drop_column("projects", "github_repo_id")
    op.drop_column("projects", "repository_url")

    # 9. Add visibility column to repositories
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
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {col}" f" TYPE varchar(255) USING {col}::text"
        )

    for table, col, _, _ in FK_TABLES:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {col}" f" TYPE varchar(255) USING {col}::text"
        )

    op.execute("ALTER TABLE projects ALTER COLUMN id TYPE varchar(255) USING id::text")

    # Re-create FK constraints
    for table, col, _, fk_name in FK_TABLES:
        op.create_foreign_key(fk_name, table, "projects", [col], ["id"])
