"""Project model."""

import uuid

from sqlalchemy import JSON, ForeignKey, Integer, String, Uuid
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column

from shared.contracts.dto.project import ProjectStatus  # Single source of truth

from .base import Base


class Project(Base):
    """Project model - tracks generated projects."""

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(40), unique=True, index=True)

    status: Mapped[str] = mapped_column(String(50), default=ProjectStatus.DRAFT.value)

    # The run that initiated this project's work — a live harness run, a matrix
    # combination, or the request the PO agent opened the project for. This is
    # the single place a run identity enters the system: it is supplied by
    # whoever starts the run, at creation, and is never derived or filled in
    # later. Every engineering and QA message carries it from here, and every
    # worker container is stamped with it as `com.codegen.run.id`.
    #
    # NULL means exactly one thing: a row that predates run ownership, whose
    # initiating run was never recorded and cannot be reconstructed. Nothing
    # writes NULL — `ProjectCreate` requires the field — and nothing fills it
    # in afterwards. Such a project cannot create workers: every producer
    # reads it through `require_initiating_run`, which refuses rather than
    # inventing a run id that would then be stamped on containers as if it
    # were one.
    initiating_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    config: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON), default=dict)

    # Project specification from .project-spec.yaml (machine-readable)
    project_spec: Mapped[dict | None] = mapped_column(MutableDict.as_mutable(JSON), nullable=True)

    # Owner (User ID) — every project must have an owner
    owner_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
