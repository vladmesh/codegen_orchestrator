"""Telegram bot token validation contracts.

The verdict is the spine for token checks: today it carries the format check and
Telegram's getMe, later layers (uniqueness across projects, external webhook/poller
detection) append their own `TokenCheck` and can set `reason_code` without changing
the shape the PO agent sees.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class TokenVerdictStatus(StrEnum):
    """Outcome of the whole validation chain."""

    OK = "ok"
    REJECTED = "rejected"


class TokenCheckName(StrEnum):
    """Validation layer that produced a check result."""

    FORMAT = "format"
    TELEGRAM_GET_ME = "telegram_get_me"


class TokenRejectionReason(StrEnum):
    """Why a token was rejected. New layers add their own codes here."""

    MALFORMED = "malformed"
    INVALID_TOKEN = "invalid_token"  # noqa: S105 — a reason code, not a secret
    NO_USERNAME = "no_username"
    TELEGRAM_UNREACHABLE = "telegram_unreachable"


class TokenCheck(BaseModel):
    """Result of one validation layer."""

    model_config = ConfigDict(extra="forbid")

    name: TokenCheckName
    passed: bool
    reason_code: TokenRejectionReason | None = None
    detail: str = ""


class TelegramTokenVerdict(BaseModel):
    """Typed verdict returned by the token validation endpoint.

    `user_message` is safe to show to the user as-is — it never contains the token.
    """

    model_config = ConfigDict(extra="forbid")

    status: TokenVerdictStatus
    reason_code: TokenRejectionReason | None = None
    user_message: str
    bot_username: str | None = None
    checks: list[TokenCheck] = Field(default_factory=list)


class TelegramTokenValidateRequest(BaseModel):
    """Request body for the token validation endpoint."""

    model_config = ConfigDict(extra="forbid")

    token: str
