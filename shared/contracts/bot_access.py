"""Temporary QA identity contract shared with deployment supervision."""

# The current template still exposes this revocable QA slot. It is deliberately
# separate from permanent owner access, which uses USERS_GRANT_CAPABILITY.
TEST_IDENTITY_ENV_KEY = "TG_BOT_TEST_TELEGRAM_ID"

# The Telegram account QA talks to bots from. One account, so a grant of the
# test slot names this id and nothing else.
QA_TEST_TELEGRAM_ID = 8202532144
