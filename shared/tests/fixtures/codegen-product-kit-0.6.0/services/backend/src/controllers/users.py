from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from services.backend.src.app.models.user import UserChannel
from services.backend.src.app.repositories.user import UserRepository
from services.backend.src.generated.protocols import UsersControllerProtocol
from shared.generated.schemas import Status, UserAccess, UserGrant, UserRevoke


def _get_repo(session: AsyncSession) -> UserRepository:
    return UserRepository(session)


def _to_access(identity: UserChannel) -> UserAccess:
    return UserAccess(
        user_id=identity.user_id,
        status=Status(identity.user.status.value),
        channel=identity.channel,
        external_id=identity.external_id,
    )


class UsersController(UsersControllerProtocol):
    """Implementation of the generated user capability contract."""

    async def grant(self, session: AsyncSession, payload: UserGrant) -> UserAccess:
        """Create or reactivate an external identity without duplicating it."""

        identity = await _get_repo(session).grant(payload.channel, payload.external_id)
        return _to_access(identity)

    async def revoke(self, session: AsyncSession, payload: UserRevoke) -> UserAccess:
        """Deactivate an existing external identity without creating it."""

        identity = await _get_repo(session).revoke(payload.channel, payload.external_id)
        if identity is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User identity not found"
            )
        return _to_access(identity)

    async def resolve(self, session: AsyncSession, channel: str, external_id: str) -> UserAccess:
        identity = await _get_repo(session).get_channel(channel, external_id)
        if identity is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User identity not found"
            )
        return _to_access(identity)
