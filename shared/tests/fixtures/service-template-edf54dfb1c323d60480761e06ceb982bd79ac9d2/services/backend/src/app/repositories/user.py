"""User and channel-identity repository helpers."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from ..models.user import User, UserChannel, UserStatus


class UserRepository:
    """Data access methods for users and their external identities."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_channel(self, channel: str, external_id: str) -> UserChannel | None:
        stmt = (
            select(UserChannel)
            .options(joinedload(UserChannel.user))
            .where(
                UserChannel.channel == channel,
                UserChannel.external_id == external_id,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def grant(self, channel: str, external_id: str) -> UserChannel:
        """Find an identity or create it, leaving its user active."""

        identity = await self.get_channel(channel, external_id)
        if identity is not None:
            identity.user.status = UserStatus.ACTIVE
            await self.session.flush()
            return identity

        try:
            async with self.session.begin_nested():
                user = User(status=UserStatus.ACTIVE)
                identity = UserChannel(user=user, channel=channel, external_id=external_id)
                self.session.add(identity)
                await self.session.flush()
        except IntegrityError:
            # A concurrent grant won the unique identity insert. Treat that as
            # the same idempotent operation rather than returning a conflict.
            identity = await self.get_channel(channel, external_id)
            if identity is None:
                raise
            identity.user.status = UserStatus.ACTIVE
            await self.session.flush()
        return identity

    async def revoke(self, channel: str, external_id: str) -> UserChannel | None:
        """Deactivate an existing identity without creating a replacement."""

        identity = await self.get_channel(channel, external_id)
        if identity is None:
            return None

        identity.user.status = UserStatus.INACTIVE
        await self.session.flush()
        return identity


__all__ = ["UserRepository"]
