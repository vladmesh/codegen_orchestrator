"""Runtime project identity helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from shared.contracts.dto.project import ProjectDTO


def project_runtime_slug(project: ProjectDTO) -> str:
    """Return the immutable runtime identifier from a project DTO."""
    return project.slug


def project_spec_runtime_slug(project_spec: Mapping[str, Any]) -> str:
    """Return the immutable runtime identifier from serialized project state."""
    slug = project_spec.get("slug")
    if not isinstance(slug, str) or not slug:
        raise RuntimeError("project slug is required for runtime operations")
    return slug
