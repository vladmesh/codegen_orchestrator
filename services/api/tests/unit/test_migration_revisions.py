"""Alembic revision metadata must be unambiguous."""

from pathlib import Path
import re

_REVISION = re.compile(r'^revision:\s*str\s*=\s*"([^"]+)"', re.MULTILINE)


def test_migration_revision_ids_are_unique() -> None:
    versions = Path(__file__).parents[2] / "migrations" / "versions"
    revision_ids = [
        match.group(1)
        for migration in versions.glob("*.py")
        if (match := _REVISION.search(migration.read_text()))
    ]

    assert len(revision_ids) == len(set(revision_ids))
