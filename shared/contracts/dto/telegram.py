"""Telegram bot token validation contracts.

The verdict is the spine for token checks: today it carries the format check,
Telegram's getMe, the external-activity probes (webhook + poller) and the
uniqueness check across projects. Later layers append their own `TokenCheck` and
can set `reason_code` without changing the shape the PO agent sees.
"""

from enum import StrEnum
import uuid

from pydantic import BaseModel, ConfigDict, Field


class TokenVerdictStatus(StrEnum):
    """Outcome of the whole validation chain."""

    OK = "ok"
    REJECTED = "rejected"


class TokenCheckName(StrEnum):
    """Validation layer that produced a check result."""

    FORMAT = "format"
    TELEGRAM_GET_ME = "telegram_get_me"
    TELEGRAM_WEBHOOK = "telegram_webhook"
    TELEGRAM_POLLER = "telegram_poller"
    PROJECT_UNIQUENESS = "project_uniqueness"


class TokenRejectionReason(StrEnum):
    """Why a token was rejected. New layers add their own codes here."""

    MALFORMED = "malformed"
    INVALID_TOKEN = "invalid_token"  # noqa: S105 — a reason code, not a secret
    NO_USERNAME = "no_username"
    TELEGRAM_UNREACHABLE = "telegram_unreachable"
    WEBHOOK_ACTIVE = "webhook_active"
    POLLER_ACTIVE = "poller_active"
    # The bot is held by another live project of the same user: nameable, actionable.
    BOUND_TO_OWN_PROJECT = "bound_to_own_project"
    # Held by someone else's project: the user learns nothing beyond "taken".
    BOUND_ELSEWHERE = "bound_elsewhere"


class TokenCheck(BaseModel):
    """Result of one validation layer."""

    model_config = ConfigDict(extra="forbid")

    name: TokenCheckName
    passed: bool
    reason_code: TokenRejectionReason | None = None
    detail: str = ""


class TelegramTokenVerdict(BaseModel):
    """Typed verdict returned by the token validation endpoint.

    `user_message` is safe to show to the user as-is: it never contains the token,
    and it names another project only when that project belongs to the same user.
    """

    model_config = ConfigDict(extra="forbid")

    status: TokenVerdictStatus
    reason_code: TokenRejectionReason | None = None
    user_message: str
    bot_username: str | None = None
    # Set only for BOUND_TO_OWN_PROJECT, so the PO agent can offer to continue there.
    conflict_project_id: uuid.UUID | None = None
    checks: list[TokenCheck] = Field(default_factory=list)


class TelegramTokenValidateRequest(BaseModel):
    """Request body for the token validation endpoint."""

    model_config = ConfigDict(extra="forbid")

    token: str
