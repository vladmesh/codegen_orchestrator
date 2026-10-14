"""The one fake Telegram bot token every test uses.

The secret part passes the product's format check (`<digits>:[A-Za-z0-9_-]{30,}`)
and is not shaped like a real token: a real one is `AA` followed by 33 more
characters, which is what GitHub's secret scanner matches. The Telegram docs'
example secret, which the tests used before, raised an alert per test file.
"""

FAKE_BOT_TOKEN_SECRET = "fake-test-bot-token-not-a-real-secret"  # noqa: S105


def fake_bot_token(bot_id: int = 123456789) -> str:
    """`<bot_id>:<fake secret>`; tests that need distinct bots pass distinct ids."""
    return f"{bot_id}:{FAKE_BOT_TOKEN_SECRET}"
