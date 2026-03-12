"""Project model."""

import uuid

from sqlalchemy import JSON, ForeignKey, Integer, String, Uuid
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column

from shared.contracts.dto.project import ProjectStatus, ServiceStatus  # Single source of truth

from .base import Base


class Project(Base):
    """Project model - tracks generated projects."""

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255))

    status: Mapped[str] = mapped_column(String(50), default=ProjectStatus.DRAFT.value)
    service_status: Mapped[str] = mapped_column(
        String(50), default=ServiceStatus.NOT_DEPLOYED.value
    )

    config: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON), default=dict)

    # Project specification from .project-spec.yaml (machine-readable)
    project_spec: Mapped[dict | None] = mapped_column(MutableDict.as_mutable(JSON), nullable=True)

    # Owner (User ID) — every project must have an owner
    owner_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
