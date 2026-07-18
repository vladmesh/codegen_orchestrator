"""Split project display title from runtime slug.

Revision ID: c7d8e9f0a1b2
Revises: b1c2d3e4f5a6
Create Date: 2026-07-18 20:20:00.000000

Existing development rows are backfilled in place: title copies the old name,
and slug is generated from title plus the first four hex characters of id.
The slug column is unique and indexed as a guardrail; uniqueness comes from id.
"""

from collections.abc import Sequence
import re

from alembic import op
import sqlalchemy as sa

revision: str = "c7d8e9f0a1b2"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SLUG_MAX_LENGTH = 40


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower())
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")


def _generate_slug(title: str, project_id: str) -> str:
    suffix = project_id.replace("-", "")[:4]
    base = _slugify(title)
    prefix = "" if base and base[0].isalpha() else "p"

    reserved = len(suffix) + 1
    if prefix:
        reserved += len(prefix) + (1 if base else 0)

    base = base[: max(SLUG_MAX_LENGTH - reserved, 0)].strip("-")
    if prefix and base:
        return f"{prefix}-{base}-{suffix}"
    if prefix:
        return f"{prefix}-{suffix}"
    return f"{base}-{suffix}"


def upgrade() -> None:
    op.add_column("projects", sa.Column("title", sa.String(length=255), nullable=True))
    op.add_column("projects", sa.Column("slug", sa.String(length=40), nullable=True))

    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, name FROM projects")).mappings()
    for row in rows:
        title = row["name"]
        slug = _generate_slug(title, str(row["id"]))
        bind.execute(
            sa.text("UPDATE projects SET title = :title, slug = :slug WHERE id = :id"),
            {"title": title, "slug": slug, "id": row["id"]},
        )

    op.alter_column("projects", "title", nullable=False)
    op.alter_column("projects", "slug", nullable=False)
    op.create_index(op.f("ix_projects_slug"), "projects", ["slug"], unique=True)
    op.drop_column("projects", "name")


def downgrade() -> None:
    op.add_column("projects", sa.Column("name", sa.String(length=255), nullable=True))
    op.execute("UPDATE projects SET name = title")
    op.alter_column("projects", "name", nullable=False)
    op.drop_index(op.f("ix_projects_slug"), table_name="projects")
    op.drop_column("projects", "slug")
    op.drop_column("projects", "title")
