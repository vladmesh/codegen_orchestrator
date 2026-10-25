"""A product-local kit package a live story built, as administrators read it."""

from datetime import datetime
import uuid

from pydantic import ConfigDict

from shared.contracts.dto.catalog_install import SHA, Distribution, Name, Strict, Version
from shared.contracts.dto.local_install import ProductPath


class PackageCandidateRead(Strict):
    """Unreviewed code exclusive to `project_id`: no catalog, planner or other product reads it.

    `updated_at` is when the newest live story recorded its commit; `created_at` the first.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    project_id: uuid.UUID
    story_id: str
    repository_id: str
    source_task_id: str
    install_task_id: str
    path: ProductPath
    package_name: Name
    package_distribution: Distribution
    package_version: Version
    commit: SHA
    core_version: Version
    python_version: Version
    tooling_commit: SHA
    capability: str
    rationale: str
    created_at: datetime
    updated_at: datetime
