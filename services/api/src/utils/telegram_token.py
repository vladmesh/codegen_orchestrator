"""Server-side Telegram bot token validation.

One door for token checks: the chain runs here, the caller gets a typed verdict.
Later layers plug in as extra checks appended to `verdict.checks`, callers keep
reading `status` / `reason_code`.

The webhook and poller layers detect a bot already running on the token outside
our system (a user who started it at home and forgot). They are best-effort by
nature: a set webhook is reported deterministically by `getWebhookInfo`, and a
live long-poller shows up as a 409 from a `getUpdates` probe. A bot that exists
but is idle right now looks exactly like a fresh token, and passes.

The last layer is inside our system: the same bot already bound to another live
project. The lookup and the owner comparison happen here, server-side; what
leaves the function is a verdict that names another project only when the user
asking already owns it.
"""

from __future__ import annotations

from http import HTTPStatus
import re

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.telegram import (
    BotLiveness,
    BotLivenessState,
    TelegramTokenVerdict,
    TokenCheck,
    TokenCheckName,
    TokenRejectionReason,
    TokenVerdictStatus,
)
from shared.models import Project, Repository

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

# The only two ways the Bot API refuses a token on `getMe`: a revoked or wrong
# secret is 401, and a token whose shape does not address a bot at all leaves the
# request with no method to answer, which is 404. Those are statements about this
# bot. Every other non-OK status is Telegram declining the request — flood
# control most of all — and is not one, which is why this set is a closed
# allow-list rather than "anything below 500".
TOKEN_REFUSED_STATUSES = frozenset({HTTPStatus.UNAUTHORIZED, HTTPStatus.NOT_FOUND})


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

    # A 429 or a 5xx here says nothing about the token, so it reads as "Telegram is
    # having a moment", not as a verdict on the bot.
    try:
        data = resp.json()
    except ValueError as e:
        logger.warning("telegram_webhook_probe_unparseable", status=resp.status_code)
        return _unreachable(TokenCheckName.TELEGRAM_WEBHOOK, str(e), preceding)

    if resp.status_code != HTTPStatus.OK or not data.get("ok"):
        description = data.get("description", "Unknown error")
        logger.warning(
            "telegram_webhook_probe_failed", status=resp.status_code, description=description
        )
        return _unreachable(TokenCheckName.TELEGRAM_WEBHOOK, description, preceding)

    url = data["result"]["url"]
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


async def _check_not_bound_elsewhere(
    db: AsyncSession,
    project: Project,
    bot_username: str,
    preceding: list[TokenCheck],
) -> TelegramTokenVerdict | TokenCheck:
    """Refuse a bot that another live project already holds.

    The binding lives on the repository row (`bot_username`), so that is what we
    look up. An archived project has let go of its bot; anything else still owns
    it. The project being bound is excluded from the search: re-sending the same
    token to the same project is an iteration, not a conflict.
    """
    query = (
        select(Project.id, Project.title, Project.owner_id)
        .join(Repository, Repository.project_id == Project.id)
        .where(
            Repository.bot_username == bot_username,
            Project.id != project.id,
            Project.status != ProjectStatus.ARCHIVED.value,
        )
    )
    holders = (await db.execute(query)).all()
    if not holders:
        return TokenCheck(name=TokenCheckName.PROJECT_UNIQUENESS, passed=True)

    # A foreign holder wins over the user's own: if someone else is on this bot,
    # "continue in your other project" would be the wrong advice, and the refusal
    # has to stay generic either way.
    foreign = [h for h in holders if h.owner_id != project.owner_id]
    if foreign:
        logger.info(
            "telegram_token_bound_elsewhere",
            project_id=str(project.id),
            bot_username=bot_username,
            holder_project_ids=[str(h.id) for h in foreign],
        )
        return _rejected(
            TokenCheck(
                name=TokenCheckName.PROJECT_UNIQUENESS,
                passed=False,
                reason_code=TokenRejectionReason.BOUND_ELSEWHERE,
                detail="Bot is bound to a project of another owner",
            ),
            "This bot is already in use. Create a new bot in @BotFather and send its token.",
            preceding=preceding,
        )

    own = holders[0]
    logger.info(
        "telegram_token_bound_to_own_project",
        project_id=str(project.id),
        bot_username=bot_username,
        holder_project_id=str(own.id),
    )
    return TelegramTokenVerdict(
        status=TokenVerdictStatus.REJECTED,
        reason_code=TokenRejectionReason.BOUND_TO_OWN_PROJECT,
        user_message=(
            f'This bot is already connected to your project "{own.title}". '
            "Continue there, or free the token from that project and send it again."
        ),
        conflict_project_id=own.id,
        checks=[
            *preceding,
            TokenCheck(
                name=TokenCheckName.PROJECT_UNIQUENESS,
                passed=False,
                reason_code=TokenRejectionReason.BOUND_TO_OWN_PROJECT,
                detail=f"Bot is bound to project {own.id}",
            ),
        ],
    )


async def validate_telegram_token(
    token: str,
    *,
    db: AsyncSession,
    project: Project,
) -> TelegramTokenVerdict:
    """Run the validation chain over a raw token and return a typed verdict.

    `project` is the project the token is being bound to: it scopes the uniqueness
    layer, which needs both its id and its owner.
    """
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

    # Last, and only now: getMe gave us the username the binding is keyed on.
    outcome = await _check_not_bound_elsewhere(db, project, bot_username, checks)
    if isinstance(outcome, TelegramTokenVerdict):
        return outcome
    checks.append(outcome)

    return TelegramTokenVerdict(
        status=TokenVerdictStatus.OK,
        user_message=(f"Token is valid. Bot: @{bot_username} (https://t.me/{bot_username})."),
        bot_username=bot_username,
        checks=checks,
    )


def _retry_after(data: dict) -> int | None:
    """How long Telegram asked the caller to wait, if it asked at all.

    `ResponseParameters.retry_after` — https://core.telegram.org/bots/api#responseparameters
    — is what flood control sends back, and the whole reason a rate-limited
    answer is retryable rather than a verdict. It is optional in the Bot API and
    absent from most errors, so its absence is ordinary and reported as `None`
    rather than guessed at.
    """
    parameters = data.get("parameters")
    if not isinstance(parameters, dict):
        return None
    retry_after = parameters.get("retry_after")
    if isinstance(retry_after, bool) or not isinstance(retry_after, int) or retry_after <= 0:
        return None
    return retry_after


async def bot_liveness(token: str) -> BotLiveness:
    """Ask Telegram whether this token still opens a live bot, right now.

    The same `getMe` the binding chain runs, asked on its own and for a different
    question: binding asks whether a token may be accepted, this asks whether the
    bot a caller is about to test answers at all. It is deliberately the only
    layer here — a webhook or another poller is somebody using the bot, not the
    bot being dead — and it returns a state rather than a verdict, because the
    caller is the platform, not the user.

    The token is read here and nowhere else. What comes back carries the username
    Telegram reported and no credential.
    """
    async with httpx.AsyncClient() as http:
        try:
            resp = await http.get(
                f"https://api.telegram.org/bot{token}/getMe",
                timeout=TELEGRAM_API_TIMEOUT,
            )
        except httpx.HTTPError as e:
            logger.warning("telegram_bot_liveness_unreachable", error=str(e))
            return BotLiveness(
                state=BotLivenessState.TELEGRAM_UNREACHABLE,
                detail=f"getMe request failed: {e}",
            )

        try:
            data = resp.json()
        # A body the Bot API never sends, but a proxy in front of it might.
        except ValueError:
            return BotLiveness(
                state=BotLivenessState.TELEGRAM_UNREACHABLE,
                detail=f"getMe returned HTTP {resp.status_code} with a body that is not JSON",
            )

    if resp.status_code != HTTPStatus.OK or not data.get("ok"):
        description = data.get("description", "no description")
        # Only the Bot API's own refusal of the token is evidence about the bot,
        # and refusal has these two spellings and no others. Everything else —
        # 429 flood control, a 5xx while Telegram is unwell, a gateway answering
        # for it — is Telegram declining to answer this request. A declined
        # request establishes nothing about the bot behind the token, so it is
        # never reported as one being dead.
        if resp.status_code in TOKEN_REFUSED_STATUSES:
            logger.info("telegram_bot_not_live", status=resp.status_code, description=description)
            return BotLiveness(
                state=BotLivenessState.NOT_LIVE,
                detail=f"Telegram refused the stored token: HTTP {resp.status_code}, {description}",
            )
        retry_after = _retry_after(data)
        logger.warning(
            "telegram_bot_liveness_declined",
            status=resp.status_code,
            description=description,
            retry_after=retry_after,
        )
        waited = f", and asked for {retry_after}s before a retry" if retry_after else ""
        return BotLiveness(
            state=BotLivenessState.TELEGRAM_UNREACHABLE,
            retry_after=retry_after,
            detail=f"getMe returned HTTP {resp.status_code}: {description}{waited}",
        )

    bot_username = data.get("result", {}).get("username")
    if not bot_username:
        return BotLiveness(
            state=BotLivenessState.NOT_LIVE,
            detail="Telegram accepted the stored token but reported no bot username",
        )
    return BotLiveness(
        state=BotLivenessState.ALIVE,
        bot_username=bot_username,
        detail=f"getMe answered as @{bot_username}",
    )
