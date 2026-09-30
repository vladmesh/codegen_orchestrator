"""Whether a Telethon session is the QA Telegram account, asked one way.

Two places need the answer and they must not disagree: the stand preflight,
which proves the stand's session before a paid suite spends anything, and the
QA runtime, which proves it for every run before it hands the session to the
QA executor's sandbox. Both call `prove_qa_identity`; a second copy of the
check would prove the copy.

Stdlib-only at import time: the stand preflight imports this on a bare
`python3`. The client is anything with Telethon's async surface.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

from shared.contracts.bot_access import QA_TEST_TELEGRAM_ID

SESSION_UNAUTHORIZED = "telethon_session_unauthorized"
IDENTITY_MISMATCH = "telethon_identity_mismatch"
# One bound per Telegram round trip. A hung MTProto connection is a refusal
# with its stage named, never a step that waits for an outer timeout.
CALL_TIMEOUT_SECONDS = 30


class IdentityNotProven(Exception):
    """The session is not proven to be the QA account. `detail` never quotes a secret."""

    def __init__(self, reason: str, detail: str, user_id: int | None = None) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.user_id = user_id


async def bounded(awaitable: Awaitable[Any], reason: str, stage: str, timeout: float) -> Any:
    """Await one Telegram call; a hang or an error is this stage's refusal.

    Only the exception's class name is kept: its text may quote what it was
    handed, and that can be the session.
    """
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except TimeoutError:
        raise IdentityNotProven(reason, f"{stage} did not answer in {timeout}s") from None
    except Exception as exc:  # noqa: BLE001 - every Telethon failure is this stage's refusal
        raise IdentityNotProven(reason, f"{stage} failed: {type(exc).__name__}") from None


async def prove_qa_identity(client: Any, *, timeout: float = CALL_TIMEOUT_SECONDS) -> int:
    """Connect *client* and prove it is authorized as `QA_TEST_TELEGRAM_ID`.

    Returns the proven user id. Raises `IdentityNotProven` with
    `SESSION_UNAUTHORIZED` or `IDENTITY_MISMATCH` otherwise. The caller owns the
    connection and disconnects it.
    """
    await bounded(client.connect(), SESSION_UNAUTHORIZED, "connect", timeout)
    if not await bounded(
        client.is_user_authorized(), SESSION_UNAUTHORIZED, "authorization check", timeout
    ):
        raise IdentityNotProven(SESSION_UNAUTHORIZED, "the session is not authorized")
    me = await bounded(client.get_me(), SESSION_UNAUTHORIZED, "get_me", timeout)
    if me is None:
        raise IdentityNotProven(SESSION_UNAUTHORIZED, "get_me returned no user")
    user_id = int(me.id)
    if user_id != QA_TEST_TELEGRAM_ID:
        raise IdentityNotProven(
            IDENTITY_MISMATCH,
            "the session is not the QA identity the QA runtime's /start probe expects",
            user_id,
        )
    return user_id
