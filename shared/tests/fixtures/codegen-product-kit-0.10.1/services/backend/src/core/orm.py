"""Declarative ORM types with no runtime settings or engine dependency."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, TypeDecorator, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class TzAwareDateTime(TypeDecorator):
    """Restore UTC after SQLite drops tzinfo on round-trip."""

    impl = DateTime
    cache_ok = True

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    pass


class CreatedAtMixin:
    """Mixin that adds created_at timestamp."""

    created_at: Mapped[datetime] = mapped_column(
        TzAwareDateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ORMBase(CreatedAtMixin, Base):
    """Common columns shared by all persisted models (created_at + updated_at)."""

    __abstract__ = True

    updated_at: Mapped[datetime] = mapped_column(
        TzAwareDateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
