"""Telegram bot audience parsing shared with deploy-time validation."""

# The contract literals the generated bot reads. The audience is product policy;
# the test identity is the single extra id a QA run borrows and gives back.
BOT_AUDIENCE_ENV_KEY = "TG_BOT_ALLOWED_TELEGRAM_IDS"
TEST_IDENTITY_ENV_KEY = "TG_BOT_TEST_TELEGRAM_ID"

# The Telegram account QA talks to bots from. One account, so a grant of the
# test slot names this id and nothing else.
QA_TEST_TELEGRAM_ID = 8202532144


def parse_allowed_telegram_ids(value: str) -> set[int]:
    """Return the integer IDs a service-template audience value enables.

    The template tolerates malformed comma-separated chunks and ignores them.
    The orchestrator uses this parser at its policy boundaries so an audience
    that the template would treat as empty cannot turn a private bot public.
    """
    ids: set[int] = set()
    for chunk in value.split(","):
        try:
            ids.add(int(chunk.strip()))
        except ValueError:
            continue
    return ids


def bot_admits(*, audience: str, test_identity: str, telegram_id: int) -> bool:
    """Whether the deployed bot lets *telegram_id* in, given these two values.

    This is the template's admission rule, kept next to the parser it already
    shares: an empty audience is the public bot, otherwise the audience decides,
    and the test slot admits exactly the id it holds. The orchestrator uses it to
    check what a deploy actually ships, so "the access was revoked" means the
    identity is refused rather than merely absent from a config.
    """
    allowed = parse_allowed_telegram_ids(audience)
    if not allowed:
        return True
    if telegram_id in allowed:
        return True
    return telegram_id in parse_allowed_telegram_ids(test_identity)


def project_bot_audience(config: dict | None) -> str:
    """The audience a project has chosen, or the empty (public) audience."""
    bot_access = (config or {}).get("bot_access")
    if not isinstance(bot_access, dict):
        return ""
    audience = bot_access.get("allowed_telegram_ids")
    return audience if isinstance(audience, str) else ""
