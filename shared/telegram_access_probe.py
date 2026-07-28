"""The /start probe that decides whether a deployed bot admits the QA identity.

QA asks the bot itself rather than reading configuration: the only evidence that
temporary access was really granted, or really taken back, is what the bot
answers the test account. The probe runs on the QA server, where the Telethon
credentials of that account live, and reports a denial as a typed blocker.

It lives in ``shared`` so that the QA consumer and the live check that proves a
revocation both send the same request and read the same answer. A copy in the
test would prove the copy.
"""

from __future__ import annotations

import shlex

from shared.contracts.bot_access import QA_TEST_TELEGRAM_ID
from shared.contracts.dto.run_result import QABlocker, QABlockerCategory

# How long the probe waits for the bot to answer /start before calling it silence.
TELEGRAM_ACCESS_PROBE_TIMEOUT = 10

DENIAL_MARKER = "telegram_access_denied:"
IDENTITY_MISMATCH_MARKER = "telegram_identity_mismatch:"
PROBE_PASSED_MARKER = "telegram_access_probe_passed"


def build_access_probe_command(bot_username: str, telethon_env_file: str) -> str:
    """Shell command that sends /start to *bot_username* as the QA account.

    Exit 0 means the bot answered something other than a refusal, exit 2 a
    refusal, exit 3 the wrong Telegram account. The account is checked first: a
    probe run from another identity would answer a question nobody asked.
    """
    return (
        f"set -a && . {telethon_env_file} && set +a; "
        "/opt/qa-runner/venv/bin/python3 -c "
        + shlex.quote(
            "import os\n"
            "from telethon.sync import TelegramClient\n"
            "from telethon.sessions import StringSession\n"
            "import sys\n"
            "import time\n"
            "client = TelegramClient(StringSession(os.environ['TELETHON_SESSION']), "
            "int(os.environ['TELETHON_API_ID']), os.environ['TELETHON_API_HASH'])\n"
            "client.start()\n"
            "try:\n"
            "    me = client.get_me()\n"
            f"    if me.id != {QA_TEST_TELEGRAM_ID}:\n"
            f"        print('{IDENTITY_MISMATCH_MARKER}expected="
            f"{QA_TEST_TELEGRAM_ID};actual=' + str(me.id))\n"
            "        sys.exit(3)\n"
            f"    bot = client.get_entity('@{bot_username}')\n"
            "    sent = client.send_message(bot, '/start')\n"
            f"    deadline = time.monotonic() + {TELEGRAM_ACCESS_PROBE_TIMEOUT}\n"
            "    while time.monotonic() < deadline:\n"
            "        replies = client.get_messages(bot, min_id=sent.id, limit=5)\n"
            "        for reply in replies:\n"
            "            if reply.out or reply.id <= sent.id:\n"
            "                continue\n"
            "            text = (reply.raw_text or reply.message or '').strip()\n"
            "            normalized = text.casefold()\n"
            "            denied = ('доступ запрещ' in normalized or 'access denied' in normalized "
            "or 'not authorized' in normalized or 'unauthorized' in normalized "
            "or 'forbidden' in normalized)\n"
            "            if denied:\n"
            f"                print('{DENIAL_MARKER}' + text[:500])\n"
            "                sys.exit(2)\n"
            "        time.sleep(1)\n"
            f"    print('{PROBE_PASSED_MARKER}')\n"
            "finally:\n"
            "    client.disconnect()"
        )
    )


def classify_access_probe(
    *, exit_status: int, stdout: str, stderr: str, bot_username: str
) -> QABlocker | None:
    """The probe's verdict: a blocker naming what the bot answered, or None.

    None means the bot did not refuse the QA account. A refusal is
    ``TELEGRAM_ACCESS_DENIED`` and carries the bot's own words, so a run that
    was locked out reads as an access decision rather than a failed script.
    """
    if exit_status == 0:
        return None

    stdout = (stdout or "").strip()
    stderr = (stderr or "").strip()
    if IDENTITY_MISMATCH_MARKER in stdout:
        category = QABlockerCategory.UNKNOWN
        received = stdout.split(IDENTITY_MISMATCH_MARKER, maxsplit=1)[1].strip()[-500:]
    elif DENIAL_MARKER in stdout:
        category = QABlockerCategory.TELEGRAM_ACCESS_DENIED
        received = stdout.split(DENIAL_MARKER, maxsplit=1)[1].strip()[-500:]
    else:
        category = QABlockerCategory.UNKNOWN
        received = stderr or stdout or "Telegram probe failed"
    return QABlocker(
        category=category,
        attempted="verify the deterministic Telegram QA identity and send /start access probe",
        sent=f"Telegram /start to @{bot_username}",
        received=received,
    )
