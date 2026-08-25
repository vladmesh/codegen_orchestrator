"""One-time promotional access codes."""

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class PromoCode(Base):
    """A code that atomically creates a user and their initial budget policy."""

    __tablename__ = "promo_codes"
    __table_args__ = (
        CheckConstraint("credits_microusd >= 0", name="ck_promo_code_credits"),
        CheckConstraint(
            "attempt_reservation_microusd > 0", name="ck_promo_code_attempt_reservation"
        ),
        UniqueConstraint("redeemed_by_user_id", name="uq_promo_code_redeemed_by_user"),
        Index("uq_promo_codes_normalized_code", text("upper(code)"), unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(255), nullable=False)
    credits_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempt_reservation_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    redeemed_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
