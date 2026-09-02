"""Persistent product settings, isolated by explicit setting scope."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, CheckConstraint, Enum, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from services.backend.src.core.orm import ORMBase


class SettingScope(StrEnum):
    """The subject boundary for a stored setting."""

    PRODUCT = "product"
    USER = "user"


class Setting(ORMBase):
    """One validated value for a declared key and subject boundary."""

    __tablename__ = "settings"
    __table_args__ = (
        CheckConstraint(
            "(scope = 'product' AND subject_id = 0) OR (scope = 'user' AND subject_id > 0)",
            name="ck_settings_scope_subject",
        ),
        UniqueConstraint("key", "scope", "subject_id", name="uq_settings_key_scope_subject"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    scope: Mapped[SettingScope] = mapped_column(
        Enum(
            SettingScope,
            name="setting_scope",
            values_callable=lambda scope: [member.value for member in scope],
        ),
        nullable=False,
    )
    # Product-wide settings use the non-null sentinel 0 so the database unique
    # constraint remains effective on PostgreSQL as well as SQLite.
    subject_id: Mapped[int] = mapped_column(Integer, nullable=False)
    value: Mapped[Any] = mapped_column(JSON(none_as_null=False), nullable=False)


__all__ = ["Setting", "SettingScope"]
