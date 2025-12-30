"""Project model."""

from enum import Enum

from sqlalchemy import JSON, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ProjectStatus(str, Enum):
    """Project lifecycle status."""

    # Inception
    DRAFT = "draft"
    DISCOVERED = "discovered"
    SETUP_REQUIRED = "setup_required"  # User requested activation, collecting secrets
    ESTIMATED = "estimated"

    # Materialization
    PROVISIONING = "provisioning"
    INITIALIZED = "initialized"

    # Construction
    DESIGNING = "designing"
    DESIGNED = "designed"
    IMPLEMENTING = "implementing"
    IMPLEMENTED = "implemented"
    VERIFYING = "verifying"
    VERIFIED = "verified"

    # Production
    DEPLOYING = "deploying"
    ACTIVE = "active"

    # Maintenance & Issues
    MAINTENANCE = "maintenance"
    MISSING = "missing"
    ERROR = "error"
    ARCHIVED = "archived"


class Project(Base):
    """Project model - tracks generated projects."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))

    # GitHub Repo ID is immutable, tracking the source of truth
    github_repo_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Repository URL for deployment (e.g., https://github.com/org/repo)
    repository_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    status: Mapped[str] = mapped_column(String(50), default=ProjectStatus.DRAFT.value)

    config: Mapped[dict] = mapped_column(JSON, default=dict)

    # Project specification from .project-spec.yaml (machine-readable)
    project_spec: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Owner (User ID)
    owner_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True
    )
