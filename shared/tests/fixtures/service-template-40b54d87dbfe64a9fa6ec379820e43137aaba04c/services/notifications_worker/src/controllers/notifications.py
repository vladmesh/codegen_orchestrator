from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.generated.schemas import UserAccess

logger = structlog.stdlib.get_logger()


class NotificationsController:
    """Controller for notifications logic."""

    async def on_user_granted(
        self,
        session: AsyncSession,
        payload: UserAccess,
    ) -> None:
        """Handle user registration."""
        logger.info(
            "Controller handled user granted",
            user_id=payload.user_id,
            status=payload.status,
        )
