"""The other end of the bot binding: letting a token go.

`bot_username` on the repository row is what the uniqueness check (one live project
per bot) reads. A project that is torn down has to drop it, or the check keeps the
token hostage to a dead project and the user cannot reuse their own bot.

Release runs server-side on the transitions that mean teardown (the project going
archived, the application going not_deployed after an undeploy), so no caller has to
remember it. Deleting a project needs nothing here: the rows go with it.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.crypto import decrypt_dict, encrypt_dict
from shared.models import Project, Repository

logger = structlog.get_logger()

TELEGRAM_TOKEN_KEY = "TELEGRAM_BOT_TOKEN"  # noqa: S105 — a key name, not a secret
TELEGRAM_USERNAME_KEY = "TELEGRAM_BOT_USERNAME"


async def release_bot_binding(
    db: AsyncSession,
    project: Project,
    repositories: Sequence[Repository],
    *,
    reason: str,
) -> str | None:
    """Drop the project's hold on its bot: the binding and the token behind it.

    Mutates the session, does not commit: the caller owns the transaction it is
    already in. Idempotent, so with nothing bound and no token stored it changes
    nothing and returns None, so a second archive or a redelivered undeploy is a
    no-op rather than an error.

    Returns the released bot username, or None if there was nothing to release.
    """
    released = None
    for repo in repositories:
        if repo.bot_username is not None:
            released = repo.bot_username
            repo.bot_username = None

    config = dict(project.config or {})
    stored = config.get("secrets") or {}
    secrets = decrypt_dict(stored) if stored else {}
    dropped = [key for key in (TELEGRAM_TOKEN_KEY, TELEGRAM_USERNAME_KEY) if key in secrets]
    for key in dropped:
        del secrets[key]
    if dropped:
        config["secrets"] = encrypt_dict(secrets) if secrets else {}
        project.config = config

    if released is None and not dropped:
        return None

    logger.info(
        "telegram_token_released",
        project_id=str(project.id),
        bot_username=released,
        dropped_secrets=dropped,
        reason=reason,
    )
    return released
