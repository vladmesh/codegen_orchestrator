"""Persistent ledger of fired job commands, one row per command identity."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Enum, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from services.backend.src.core.orm import ORMBase, TzAwareDateTime


class DispatchStatus(StrEnum):
    """What the core did with an accepted command."""

    DISPATCHED = "dispatched"
    UNDELIVERED = "undelivered"


class JobCommand(ORMBase):
    """One recorded fire of a declared behaviour, unique by caller identity.

    Identity is the tuple ``(fired_by_product, command_id)``. Storage uniqueness on
    that tuple is what makes a retry idempotent: a repeated fire finds the recorded
    row and returns its evidence instead of executing a second time.
    """

    __tablename__ = "job_commands"
    __table_args__ = (
        UniqueConstraint(
            "fired_by_product", "command_id", name="uq_job_commands_product_command"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    command_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    arguments: Mapped[Any] = mapped_column(JSON(none_as_null=False), nullable=False)
    fired_by_product: Mapped[str] = mapped_column(String(255), nullable=False)
    fired_by_run: Mapped[str] = mapped_column(String(255), nullable=False)
    dispatch_status: Mapped[DispatchStatus] = mapped_column(
        Enum(
            DispatchStatus,
            name="job_dispatch_status",
            values_callable=lambda status: [member.value for member in status],
        ),
        nullable=False,
    )
    accepted_at: Mapped[datetime] = mapped_column(
        TzAwareDateTime(timezone=True), nullable=False
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(
        TzAwareDateTime(timezone=True), nullable=True
    )


__all__ = ["DispatchStatus", "JobCommand"]
