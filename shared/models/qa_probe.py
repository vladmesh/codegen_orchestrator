"""A project's QA probe library entry: one probe a passed QA run executed."""

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

from shared.contracts.dto.run_result import QAProbeFileKind, QAProbePlatform

from .base import Base

_PLATFORMS = ", ".join(f"'{platform.value}'" for platform in QAProbePlatform)
_FILE_KINDS = ", ".join(f"'{kind.value}'" for kind in QAProbeFileKind)


class QAProbe(Base):
    """One stored probe, unique per project, platform and name.

    Written only from a passed QA Run's retained `probe_runs`, so its source is
    the already scrubbed and bounded record. `updated_at` is when a run last
    stored it; the project's oldest entries by it are evicted past the cap.
    """

    __tablename__ = "qa_probes"
    __table_args__ = (
        UniqueConstraint("project_id", "platform", "name", name="uq_qa_probes_project_name"),
        CheckConstraint(f"platform IN ({_PLATFORMS})", name="ck_qa_probes_platform"),
        CheckConstraint(f"file_kind IN ({_FILE_KINDS})", name="ck_qa_probes_file_kind"),
        Index("ix_qa_probes_project_updated", "project_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("projects.id"), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    file_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    origin_run_id: Mapped[str] = mapped_column(String(255), nullable=False)
