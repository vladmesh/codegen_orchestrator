"""A product-local kit package a live story built: unreviewed and exclusive to its product."""

import uuid

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class PackageCandidate(Base):
    """One row per (project, package name), at the newest live story's DONE INSTALL commit.

    `capability` and `rationale` are the source FEATURE's title and description.
    """

    __tablename__ = "package_candidates"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "package_name", name="uq_package_candidates_project_package"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("projects.id"), nullable=False)
    story_id: Mapped[str] = mapped_column(String(255), ForeignKey("stories.id"), nullable=False)
    repository_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("repositories.id"), nullable=False
    )
    source_task_id: Mapped[str] = mapped_column(String(255), ForeignKey("tasks.id"), nullable=False)
    install_task_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("tasks.id"), nullable=False
    )
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    package_name: Mapped[str] = mapped_column(String(64), nullable=False)
    package_distribution: Mapped[str] = mapped_column(String(128), nullable=False)
    package_version: Mapped[str] = mapped_column(String(255), nullable=False)
    commit: Mapped[str] = mapped_column(String(40), nullable=False)
    core_version: Mapped[str] = mapped_column(String(255), nullable=False)
    python_version: Mapped[str] = mapped_column(String(255), nullable=False)
    tooling_commit: Mapped[str] = mapped_column(String(40), nullable=False)
    capability: Mapped[str] = mapped_column(String(500), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
