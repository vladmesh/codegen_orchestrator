"""Repository model — git repository belonging to a project."""

import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from shared.contracts.dto.repository import RepositoryRole, RepositoryStatus, RepositoryVisibility

from .base import Base


class Repository(Base):
    """Repository — a git repository linked to a project."""

    __tablename__ = "repositories"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    git_url: Mapped[str] = mapped_column(String(512))
    provider_repo_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    role: Mapped[str] = mapped_column(String(50), default=RepositoryRole.PRIMARY.value)
    visibility: Mapped[str] = mapped_column(String(20), default=RepositoryVisibility.PRIVATE.value)
    is_managed: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(50), default=RepositoryStatus.ACTIVE.value)
