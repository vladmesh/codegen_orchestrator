"""Server-side Telegram bot token validation.

One door for token checks: the chain runs here, the caller gets a typed verdict.
Later layers (uniqueness, external activity detection) plug in as extra checks
appended to `verdict.checks` — callers keep reading `status` / `reason_code`.
"""

from __future__ import annotations

from http import HTTPStatus
import re

import httpx
import structlog

from shared.contracts.dto.telegram import (
    TelegramTokenVerdict,
    TokenCheck,
    TokenCheckName,
    TokenRejectionReason,
    TokenVerdictStatus,
)

logger = structlog.get_logger()

TELEGRAM_API_TIMEOUT = 10

# BotFather tokens: numeric bot id, colon, then the secret part.
BOT_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


def looks_like_bot_token(value: str) -> bool:
    """True if the value has the shape of a Telegram bot token."""
    return bool(BOT_TOKEN_RE.match(value.strip()))


def _rejected(
    check: TokenCheck,
    message: str,
    preceding: list[TokenCheck] | None = None,
) -> TelegramTokenVerdict:
    return TelegramTokenVerdict(
        status=TokenVerdictStatus.REJECTED,
        reason_code=check.reason_code,
        user_message=message,
        checks=[*(preceding or []), check],
    )


async def validate_telegram_token(token: str) -> TelegramTokenVerdict:
    """Run the validation chain over a raw token and return a typed verdict."""
    token = token.strip()

    if not looks_like_bot_token(token):
        logger.info("telegram_token_malformed")
        return _rejected(
            TokenCheck(
                name=TokenCheckName.FORMAT,
                passed=False,
                reason_code=TokenRejectionReason.MALFORMED,
                detail="Token does not match the BotFather format",
            ),
            "This does not look like a Telegram bot token. It should look like "
            "123456789:AA... — copy the whole line @BotFather sent you.",
        )

    format_check = TokenCheck(name=TokenCheckName.FORMAT, passed=True)

    try:
        async with httpx.AsyncClient() as http:
            resp = await http.get(
                f"https://api.telegram.org/bot{token}/getMe",
                timeout=TELEGRAM_API_TIMEOUT,
            )
    except httpx.HTTPError as e:
        logger.warning("telegram_token_validation_unreachable", error=str(e))
        return _rejected(
            TokenCheck(
                name=TokenCheckName.TELEGRAM_GET_ME,
                passed=False,
                reason_code=TokenRejectionReason.TELEGRAM_UNREACHABLE,
                detail=str(e),
            ),
            "Could not reach Telegram right now. Please try again in a minute.",
            preceding=[format_check],
        )

    data = resp.json()
    if resp.status_code != HTTPStatus.OK or not data.get("ok"):
        description = data.get("description", "Unknown error")
        logger.info("telegram_token_invalid", status=resp.status_code, description=description)
        return _rejected(
            TokenCheck(
                name=TokenCheckName.TELEGRAM_GET_ME,
                passed=False,
                reason_code=TokenRejectionReason.INVALID_TOKEN,
                detail=description,
            ),
            f"Telegram rejected this token: {description}. "
            "Check the token in @BotFather and send it again.",
            preceding=[format_check],
        )

    bot_username = data.get("result", {}).get("username")
    if not bot_username:
        logger.warning("telegram_token_no_username")
        return _rejected(
            TokenCheck(
                name=TokenCheckName.TELEGRAM_GET_ME,
                passed=False,
                reason_code=TokenRejectionReason.NO_USERNAME,
                detail="getMe returned no username",
            ),
            "Telegram accepted the token but returned no bot username. "
            "Recreate the token in @BotFather and send the new one.",
            preceding=[format_check],
        )

    return TelegramTokenVerdict(
        status=TokenVerdictStatus.OK,
        user_message=(f"Token is valid. Bot: @{bot_username} (https://t.me/{bot_username})."),
        bot_username=bot_username,
        checks=[
            format_check,
            TokenCheck(name=TokenCheckName.TELEGRAM_GET_ME, passed=True, detail=bot_username),
        ],
    )
