"""User and external-channel identity ORM models."""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from services.backend.src.core.orm import ORMBase


class UserStatus(StrEnum):
    """The only persisted admission decision for a user."""

    ACTIVE = "active"
    INACTIVE = "inactive"


class User(ORMBase):
    """Represents a product user independently of any delivery channel."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    status: Mapped[UserStatus] = mapped_column(
        Enum(
            UserStatus,
            name="user_status",
            values_callable=lambda status: [member.value for member in status],
        ),
        nullable=False,
        server_default=text("'inactive'"),
    )
    channels: Mapped[list[UserChannel]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserChannel(ORMBase):
    """Maps one external identity in a channel to exactly one user."""

    __tablename__ = "user_channels"
    __table_args__ = (
        UniqueConstraint("channel", "external_id", name="uq_user_channels_channel_external_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str] = mapped_column(String(256), nullable=False)
    user: Mapped[User] = relationship(back_populates="channels")


__all__ = ["User", "UserChannel", "UserStatus"]
