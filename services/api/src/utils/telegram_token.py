"""Server-side Telegram bot token validation.

One door for token checks: the chain runs here, the caller gets a typed verdict.
Later layers (uniqueness) plug in as extra checks appended to `verdict.checks` —
callers keep reading `status` / `reason_code`.

The last two layers detect a bot already running on the token outside our system
(a user who started it at home and forgot). They are best-effort by nature: a set
webhook is reported deterministically by `getWebhookInfo`, and a live long-poller
shows up as a 409 from a `getUpdates` probe. A bot that exists but is idle right
now looks exactly like a fresh token, and passes.
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

# The getUpdates probe must not sit in someone else's long poll, so it asks for no
# server-side wait at all and gives up quickly on its own.
EXTERNAL_PROBE_TIMEOUT = 5

# Same wording for both external-activity cases: we know something answers on this
# token, we do not know whose it is, so the message does not guess.
EXTERNAL_ACTIVITY_MESSAGE = (
    "Something is already running on this token. Stop it and send the token again, "
    "or send a token for a different bot."
)

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


def _unreachable(
    check_name: TokenCheckName,
    error: str,
    preceding: list[TokenCheck],
) -> TelegramTokenVerdict:
    return _rejected(
        TokenCheck(
            name=check_name,
            passed=False,
            reason_code=TokenRejectionReason.TELEGRAM_UNREACHABLE,
            detail=error,
        ),
        "Could not reach Telegram right now. Please try again in a minute.",
        preceding=preceding,
    )


async def _check_no_webhook(
    http: httpx.AsyncClient,
    token: str,
    preceding: list[TokenCheck],
) -> TelegramTokenVerdict | TokenCheck:
    """Read-only: a non-empty webhook url means someone already wired this bot up."""
    try:
        resp = await http.get(
            f"https://api.telegram.org/bot{token}/getWebhookInfo",
            timeout=EXTERNAL_PROBE_TIMEOUT,
        )
    except httpx.HTTPError as e:
        logger.warning("telegram_webhook_probe_unreachable", error=str(e))
        return _unreachable(TokenCheckName.TELEGRAM_WEBHOOK, str(e), preceding)

    url = resp.json()["result"]["url"]
    if url:
        logger.info("telegram_token_external_webhook")
        return _rejected(
            TokenCheck(
                name=TokenCheckName.TELEGRAM_WEBHOOK,
                passed=False,
                reason_code=TokenRejectionReason.WEBHOOK_ACTIVE,
                detail="getWebhookInfo returned a non-empty url",
            ),
            EXTERNAL_ACTIVITY_MESSAGE,
            preceding=preceding,
        )

    return TokenCheck(name=TokenCheckName.TELEGRAM_WEBHOOK, passed=True)


async def _check_no_poller(
    http: httpx.AsyncClient,
    token: str,
    preceding: list[TokenCheck],
) -> TelegramTokenVerdict | TokenCheck:
    """Probe getUpdates: a live long-poller on the same token makes Telegram answer 409.

    No `offset` at all. Per the Bot API, an update is confirmed only when getUpdates is
    called with an offset higher than its update_id, and a negative offset additionally
    makes all earlier updates forgotten — so any offset we could pass would either ack or
    drop someone else's backlog. Omitting it returns from the earliest unconfirmed update
    and changes nothing server-side. `timeout=0` keeps it a single short request, so
    another bot's poll loop is interrupted for one cycle at most, and `limit=1` keeps the
    foreign payload we never read down to one update.
    """
    try:
        resp = await http.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"limit": 1, "timeout": 0},
            timeout=EXTERNAL_PROBE_TIMEOUT,
        )
    except httpx.HTTPError as e:
        logger.warning("telegram_poller_probe_unreachable", error=str(e))
        return _unreachable(TokenCheckName.TELEGRAM_POLLER, str(e), preceding)

    if resp.status_code == HTTPStatus.CONFLICT:
        logger.info("telegram_token_external_poller")
        return _rejected(
            TokenCheck(
                name=TokenCheckName.TELEGRAM_POLLER,
                passed=False,
                reason_code=TokenRejectionReason.POLLER_ACTIVE,
                detail="getUpdates answered 409 Conflict",
            ),
            EXTERNAL_ACTIVITY_MESSAGE,
            preceding=preceding,
        )

    # Anything else is "no poller seen". The probe only ever proves activity, never
    # its absence, so an odd answer here is not grounds to refuse the token.
    if resp.status_code != HTTPStatus.OK:
        logger.info("telegram_poller_probe_inconclusive", status=resp.status_code)

    return TokenCheck(name=TokenCheckName.TELEGRAM_POLLER, passed=True)


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

    async with httpx.AsyncClient() as http:
        try:
            resp = await http.get(
                f"https://api.telegram.org/bot{token}/getMe",
                timeout=TELEGRAM_API_TIMEOUT,
            )
        except httpx.HTTPError as e:
            logger.warning("telegram_token_validation_unreachable", error=str(e))
            return _unreachable(TokenCheckName.TELEGRAM_GET_ME, str(e), [format_check])

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

        checks = [
            format_check,
            TokenCheck(name=TokenCheckName.TELEGRAM_GET_ME, passed=True, detail=bot_username),
        ]

        # Webhook first: with one set, getUpdates answers 409 for that reason alone and
        # the poller probe would blame the wrong thing.
        for probe in (_check_no_webhook, _check_no_poller):
            outcome = await probe(http, token, checks)
            if isinstance(outcome, TelegramTokenVerdict):
                return outcome
            checks.append(outcome)

    return TelegramTokenVerdict(
        status=TokenVerdictStatus.OK,
        user_message=(f"Token is valid. Bot: @{bot_username} (https://t.me/{bot_username})."),
        bot_username=bot_username,
        checks=checks,
    )
