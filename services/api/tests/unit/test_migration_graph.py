"""The alembic revision graph is one chain with one head.

A hand-written revision id can collide with one that already exists, or a
branch can be left with two heads. Either way `alembic upgrade head` refuses to
run and every container that migrates on start dies — a failure that only shows
up once a database is involved, long after the unit suite is green. Reading the
revision files is enough to catch it here, with no database at all.
"""

from pathlib import Path
import re

VERSIONS_DIR = Path(__file__).resolve().parents[2] / "migrations" / "versions"

_REVISION_RE = re.compile(r"^revision[^=\n]*=\s*(.+)$", re.MULTILINE)
_DOWN_REVISION_RE = re.compile(r"^down_revision[^=\n]*=\s*(.+)$", re.MULTILINE)
_LITERAL_RE = re.compile(r"['\"]([^'\"]+)['\"]")


def _revisions() -> dict[str, list[tuple[str, list[str]]]]:
    """Map every declared revision id to the files declaring it and its parents."""
    by_id: dict[str, list[tuple[str, list[str]]]] = {}
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        source = path.read_text()
        revision_match = _REVISION_RE.search(source)
        assert revision_match is not None, f"{path.name} declares no revision id"
        revision = _LITERAL_RE.findall(revision_match.group(1))[0]
        down_match = _DOWN_REVISION_RE.search(source)
        parents = _LITERAL_RE.findall(down_match.group(1)) if down_match else []
        by_id.setdefault(revision, []).append((path.name, parents))
    return by_id


def test_revision_ids_are_unique():
    by_id = _revisions()
    duplicates = {
        revision: [name for name, _ in entries]
        for revision, entries in by_id.items()
        if len(entries) > 1
    }
    assert not duplicates, f"two migrations share a revision id: {duplicates}"


def test_exactly_one_head():
    by_id = _revisions()
    parents = {parent for entries in by_id.values() for _, ps in entries for parent in ps}
    heads = sorted(set(by_id) - parents)
    assert len(heads) == 1, f"alembic has {len(heads)} heads, expected 1: {heads}"
