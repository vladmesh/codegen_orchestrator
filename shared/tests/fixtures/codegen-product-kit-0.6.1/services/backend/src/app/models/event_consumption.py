"""Durable identities of product events consumed by this service."""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from services.backend.src.core.orm import Base, CreatedAtMixin


class EventConsumption(CreatedAtMixin, Base):
    """One committed effect for an event within a consumer group."""

    __tablename__ = "event_consumptions"

    consumer_group: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
