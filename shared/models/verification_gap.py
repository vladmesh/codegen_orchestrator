"""A project's verification gap: one check a settled QA run could not perform."""

import uuid

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.contracts.dto.qa_verification import QAUnverifiedOrigin

from .base import Base

_ORIGINS = ", ".join(f"'{origin.value}'" for origin in QAUnverifiedOrigin)


class VerificationGap(Base):
    """What QA could not check (`name`), why (`reason`), for which story and run.

    Written only from a settled QA Run's own `unverified_checks`, once per
    (project, run, check): writing the same run again adds nothing. `created_at`
    is when it was written.
    """

    __tablename__ = "verification_gaps"
    __table_args__ = (
        UniqueConstraint("project_id", "run_id", "name", name="uq_verification_gaps_run_check"),
        CheckConstraint(f"origin IN ({_ORIGINS})", name="ck_verification_gaps_origin"),
        Index("ix_verification_gaps_project_created", "project_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("projects.id"), nullable=False)
    # Plain ids, not foreign keys: the gap is the project's record of what was
    # never checked, and it names the story and run it came from without
    # holding their rows in place.
    story_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
