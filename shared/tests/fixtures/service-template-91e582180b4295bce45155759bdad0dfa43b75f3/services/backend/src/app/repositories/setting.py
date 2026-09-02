"""Database access for idempotent, subject-scoped product settings."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.setting import Setting, SettingScope


class SettingRepository:
    """Read and write settings without taking ownership of the transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, key: str, scope: SettingScope, subject_id: int) -> Setting | None:
        result = await self.session.execute(
            select(Setting).where(
                Setting.key == key,
                Setting.scope == scope,
                Setting.subject_id == subject_id,
            )
        )
        return result.scalar_one_or_none()

    async def set(self, key: str, scope: SettingScope, subject_id: int, value: Any) -> Setting:
        """Create or update a value, preserving a retry of the same JSON value."""
        setting = await self.get(key, scope, subject_id)
        if setting is None:
            setting = Setting(key=key, scope=scope, subject_id=subject_id, value=value)
            self.session.add(setting)
        elif setting.value != value:
            setting.value = value
        await self.session.flush()
        return setting


__all__ = ["SettingRepository"]
