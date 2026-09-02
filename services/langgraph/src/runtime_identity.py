"""Runtime project identity helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from shared.contracts.dto.project import ProjectDTO

# Where a deployed stack lives on its target host. The base is fixed by the
# generated repository's own deploy workflow, so every module that composes a
# remote path — precheck, lifecycle, smoke, QA targeting — must derive it from
# the same value as the runtime slug it is joined with.
SERVICE_BASE_DIR = "/opt/services"


def project_runtime_slug(project: ProjectDTO) -> str:
    """Return the immutable runtime identifier from a project DTO."""
    return project.slug


def project_spec_runtime_slug(project_spec: Mapping[str, Any]) -> str:
    """Return the immutable runtime identifier from serialized project state."""
    slug = project_spec.get("slug")
    if not isinstance(slug, str) or not slug:
        raise RuntimeError("project slug is required for runtime operations")
    return slug
