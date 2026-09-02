"""The Product Brief and the requirement dispositions that release its plan.

A brief is the confirmed product intent of one project at one revision. Its
content is frozen: `confirm` stamps `confirmed_at` and nothing rewrites
`content` afterwards, because a change to what the user asked for is a new
revision of the brief, not an edit of the one an architect may already be
planning against.

Three fields make one architect the owner of an incomplete plan —
`planning_attempt_id`, `planning_attempt_active` and
`planning_attempt_heartbeat_at` — and one field, `coverage_admitted_at`, is the
immutable record that the coverage-to-dispatch boundary has been crossed. Both
are read and written only under `SELECT ... FOR UPDATE` on this row, which is
what makes the admission step idempotent and the claim a race exactly one caller
wins.
"""

from datetime import datetime
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ProductBrief(Base):
    """One immutable revision of a project's confirmed product intent."""

    __tablename__ = "product_briefs"
    __table_args__ = (UniqueConstraint("project_id", "revision", name="uq_product_brief_revision"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id"), nullable=False, index=True
    )
    #: The story this brief is planned into. Unique: one brief backs at most one
    #: story, and one story is backed by at most one brief, so "the tasks of this
    #: brief" and "the tasks of this story" name the same roster.
    story_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("stories.id"), unique=True, nullable=True, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    #: The `ProductBriefContent` document. Written once, at creation, and never
    #: updated in place — see the module docstring.
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    #: The creating caller's idempotency key, so a retried creation returns the
    #: revision it already made instead of opening a second one.
    request_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmation_request_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True
    )
    #: The immutable record of the coverage-to-dispatch boundary. Set exactly
    #: once, by the admission step, in the transaction that releases the tasks.
    coverage_admitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Exactly one live architect owns an incomplete plan. A superseded owner
    #: cannot record coverage, cannot admit, and cannot have its abandoned tasks
    #: released by its replacement's admission.
    planning_attempt_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    planning_attempt_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    planning_attempt_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class RequirementCoverage(Base):
    """How one must-requirement of a brief was disposed of by the architect.

    Either it is covered — a task was planned for it — or it was returned with a
    reason. Both are dispositions; the admission step asks only whether every
    must-requirement has one, because a returned requirement is a decision, not
    a gap.
    """

    __tablename__ = "requirement_coverages"
    __table_args__ = (
        UniqueConstraint("brief_id", "requirement_id", name="uq_requirement_coverage"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    brief_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("product_briefs.id"), nullable=False, index=True
    )
    requirement_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("tasks.id"), nullable=True)
    returned_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The planning attempt that recorded this disposition. The admission step
    #: counts only the rows its own attempt wrote, so a plan taken over from a
    #: stale architect has to be re-disposed of rather than inheriting coverage
    #: that points at the abandoned attempt's tasks.
    planning_attempt_id: Mapped[str] = mapped_column(String(128), nullable=False)
