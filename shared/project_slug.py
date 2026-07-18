"""Project slug generation."""

from __future__ import annotations

import re
import uuid

PROJECT_SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
PROJECT_SLUG_MAX_LENGTH = 40


def slugify_project_title(title: str) -> str:
    """Return the human title's filesystem-safe base slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower())
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")


def generate_project_slug(title: str, project_id: uuid.UUID) -> str:
    """Generate a stable runtime slug from title and project UUID."""
    suffix = project_id.hex
    base = slugify_project_title(title)
    prefix = "" if base and base[0].isalpha() else "p"

    reserved = len(suffix) + 1
    if prefix:
        reserved += len(prefix) + (1 if base else 0)

    max_base_length = max(PROJECT_SLUG_MAX_LENGTH - reserved, 0)
    base = base[:max_base_length].strip("-")

    if prefix and base:
        slug = f"{prefix}-{base}-{suffix}"
    elif prefix:
        slug = f"{prefix}-{suffix}"
    else:
        slug = f"{base}-{suffix}"

    if not PROJECT_SLUG_PATTERN.fullmatch(slug):
        raise ValueError(f"generated invalid project slug {slug!r}")
    return slug
