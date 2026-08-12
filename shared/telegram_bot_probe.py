"""The one way a central QA run talks to a deployed Telegram bot.

The agent never gets a Telegram session: it names a message, and the runtime
sends it as the deterministic QA account and hands back what the bot answered.
Everything the agent could otherwise do with an authorized MTProto session —
message anyone, read any dialog, act as the account — stays outside its reach
because the script is built here and the credentials never leave the runtime's
environment.

The script is a string rather than an import so that it runs in a child process:
Telethon's synchronous client cannot share the consumer's event loop, and a hung
connection has to be killable without taking the run with it.
"""

from __future__ import annotations

import json

# How long the bot is given to answer before the probe reports silence.
TELEGRAM_REPLY_TIMEOUT = 15
MAX_REPLIES = 10
REPLIES_MARKER = "telegram_replies:"


def build_bot_message_script(
    bot_username: str,
    message: str,
    *,
    wait_seconds: int = TELEGRAM_REPLY_TIMEOUT,
) -> str:
    """Python source that sends *message* to the bot and prints its replies.

    Exit 0 with a ``telegram_replies:`` line carrying a JSON list of the texts
    the bot sent after the message, oldest first. Silence is an empty list and
    still exit 0: "the bot said nothing" is a test result, not a runtime error.
    """
    return (
        "import json\n"
        "import os\n"
        "import time\n"
        "from telethon.sync import TelegramClient\n"
        "from telethon.sessions import StringSession\n"
        "client = TelegramClient(StringSession(os.environ['TELETHON_SESSION']), "
        "int(os.environ['TELETHON_API_ID']), os.environ['TELETHON_API_HASH'])\n"
        "client.start()\n"
        "try:\n"
        f"    bot = client.get_entity({json.dumps('@' + bot_username.lstrip('@'))})\n"
        f"    sent = client.send_message(bot, {json.dumps(message)})\n"
        "    replies = []\n"
        f"    deadline = time.monotonic() + {wait_seconds}\n"
        "    while time.monotonic() < deadline:\n"
        f"        found = client.get_messages(bot, min_id=sent.id, limit={MAX_REPLIES})\n"
        "        replies = [\n"
        "            (m.raw_text or m.message or '')\n"
        "            for m in reversed(found)\n"
        "            if not m.out and m.id > sent.id\n"
        "        ]\n"
        "        if replies:\n"
        "            break\n"
        "        time.sleep(1)\n"
        f"    print({json.dumps(REPLIES_MARKER)} + json.dumps(replies))\n"
        "finally:\n"
        "    client.disconnect()"
    )


def parse_bot_replies(stdout: str) -> list[str]:
    """Read the reply list the probe script printed.

    A probe that produced no marker produced no evidence, so it raises rather
    than passing an empty conversation off as "the bot stayed silent".
    """
    for line in stdout.splitlines():
        if line.startswith(REPLIES_MARKER):
            return json.loads(line.removeprefix(REPLIES_MARKER))
    raise ValueError("the Telegram probe printed no replies line")
