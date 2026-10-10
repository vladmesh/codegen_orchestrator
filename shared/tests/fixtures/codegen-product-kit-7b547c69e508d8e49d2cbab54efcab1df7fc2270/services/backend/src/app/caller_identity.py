"""Establish the verified caller identity that package routes act for.

Package routes reach this through ``codegen_kit.caller_identity``. It is the only place a
user identity is accepted from a request: a trusted in-product service presents the
caller-identity capability together with the channel identity of the user it acts for,
and the core resolves that identity through ``user_channels``.
"""

from __future__ import annotations

from secrets import compare_digest
from typing import Annotated, Final

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from services.backend.src.app.models.user import UserStatus
from services.backend.src.app.repositories.user import UserRepository
from services.backend.src.core.db import get_async_db
from services.backend.src.core.settings import get_settings

IDENTITY_CAPABILITY_HEADER: Final[str] = "X-Identity-Capability"
USER_CHANNEL_HEADER: Final[str] = "X-User-Channel"
USER_EXTERNAL_ID_HEADER: Final[str] = "X-User-External-Id"
# The persisted column widths of ``user_channels``.
MAX_CHANNEL_LENGTH: Final[int] = 64
MAX_EXTERNAL_ID_LENGTH: Final[int] = 256


def _single_header(request: Request, name: str, max_length: int) -> str | None:
    """Return exactly one non-empty printable ASCII header value, otherwise ``None``."""

    presented = request.headers.getlist(name)
    value = presented[0] if len(presented) == 1 else ""
    if 0 < len(value) <= max_length and value.isascii() and value.isprintable():
        return value
    return None


def _require_capability(request: Request) -> None:
    expected = get_settings().user_identity_capability
    presented = _single_header(request, IDENTITY_CAPABILITY_HEADER, len(expected))
    if presented is None or not compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Caller identity capability required",
        )


def _channel_identity(request: Request) -> tuple[str, str]:
    channel = _single_header(request, USER_CHANNEL_HEADER, MAX_CHANNEL_LENGTH)
    external_id = _single_header(request, USER_EXTERNAL_ID_HEADER, MAX_EXTERNAL_ID_LENGTH)
    # A colon in the channel would make the canonical "<channel>:<external_id>" ambiguous.
    if channel is None or external_id is None or ":" in channel:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Caller user identity required",
        )
    return channel, external_id


async def caller_identity(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_async_db)],
) -> str:
    """Return the canonical ``"<channel>:<external_id>"`` of the verified active caller.

    401: the capability or the identity headers are missing, repeated or malformed.
    403: the identity is unknown, or its user is not active.
    """

    _require_capability(request)
    channel, external_id = _channel_identity(request)
    identity = await UserRepository(session).get_channel(channel, external_id)
    if identity is None or identity.user.status != UserStatus.ACTIVE:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Caller is not active")
    return f"{identity.channel}:{identity.external_id}"


__all__ = [
    "IDENTITY_CAPABILITY_HEADER",
    "USER_CHANNEL_HEADER",
    "USER_EXTERNAL_ID_HEADER",
    "caller_identity",
]
