"""Telegram bot audience parsing shared with deploy-time validation."""


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
