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
PROBE_RESULT_MARKER = "telegram_probe_result:"


def _script_helpers() -> str:
    """The self-contained serializer shared by the message and callback scripts."""
    return """\
def encode_callback_data(value):
    if value is None:
        return None
    return base64.b64encode(bytes(value)).decode("ascii")


def reply_markup_evidence(markup):
    if markup is None:
        return None
    buttons = []
    for row_index, row in enumerate(getattr(markup, "rows", []) or []):
        for column_index, button in enumerate(getattr(row, "buttons", []) or []):
            buttons.append({
                "row": row_index,
                "column": column_index,
                "text": getattr(button, "text", None),
                "type": type(button).__name__,
                "callback_data": encode_callback_data(getattr(button, "data", None)),
            })
    return {"type": type(markup).__name__, "buttons": buttons}


def message_evidence(message):
    media = getattr(message, "media", None)
    media_type = type(media).__name__ if media is not None else None
    content = getattr(message, "raw_text", None)
    if content is None:
        content = getattr(message, "message", None)
    content = content or None
    is_attachment = media is not None and media_type != "MessageMediaWebPage"
    return {
        "id": message.id,
        "text": None if is_attachment else content,
        "caption": content if is_attachment else None,
        "media_type": media_type,
        "reply_markup": reply_markup_evidence(getattr(message, "reply_markup", None)),
    }


def received_replies(client, bot, minimum_id):
    found = client.get_messages(bot, min_id=minimum_id, limit=MAX_REPLIES)
    return [
        message_evidence(message)
        for message in reversed(found)
        if not message.out and message.id > minimum_id
    ]


def newest_message_id(client, bot):
    messages = client.get_messages(bot, limit=1)
    return messages[0].id if messages else 0
"""


def build_bot_message_script(
    bot_username: str,
    message: str,
    *,
    wait_seconds: int = TELEGRAM_REPLY_TIMEOUT,
) -> str:
    """Python source that sends *message* to the bot and prints structured evidence.

    The child always prints exactly one result marker. Its ``delivered`` field
    distinguishes a Telethon send failure from an observable bot reply, so the
    caller can prevent an undelivered test operation from becoming a product
    failure. Silence remains an empty reply list and is a test result.
    """
    return (
        "import base64\n"
        "import json\n"
        "import os\n"
        "import time\n"
        f"MAX_REPLIES = {MAX_REPLIES}\n"
        "from telethon.sync import TelegramClient\n"
        "from telethon.sessions import StringSession\n"
        + _script_helpers()
        + "client = None\n"
        + "result = {\n"
        "    'action': 'message',\n"
        f"    'attempted': {json.dumps(f'send {message!r} to @{bot_username.lstrip("@")}')},\n"
        f"    'sent': {json.dumps(message)},\n"
        "    'delivered': False,\n"
        "    'replies': [],\n"
        "    'callback': None,\n"
        "    'error': None,\n"
        "}\n"
        "try:\n"
        "    client = TelegramClient(StringSession(os.environ['TELETHON_SESSION']), "
        "int(os.environ['TELETHON_API_ID']), os.environ['TELETHON_API_HASH'])\n"
        "    client.start()\n"
        f"    bot = client.get_entity({json.dumps('@' + bot_username.lstrip('@'))})\n"
        f"    sent = client.send_message(bot, {json.dumps(message)})\n"
        "    result['delivered'] = True\n"
        f"    deadline = time.monotonic() + {wait_seconds}\n"
        "    while time.monotonic() < deadline:\n"
        "        result['replies'] = received_replies(client, bot, sent.id)\n"
        "        if result['replies']:\n"
        "            break\n"
        "        time.sleep(1)\n"
        "except Exception as exc:\n"
        "    result['error'] = type(exc).__name__ + ': ' + str(exc)\n"
        "finally:\n"
        "    try:\n"
        "        if client is not None:\n"
        "            client.disconnect()\n"
        "    except Exception as exc:\n"
        "        if result['error'] is None:\n"
        "            result['error'] = type(exc).__name__ + ': ' + str(exc)\n"
        f"    print({json.dumps(PROBE_RESULT_MARKER)} + json.dumps(result))"
    )


def build_bot_callback_script(
    bot_username: str,
    message_id: int,
    callback_data: str,
    *,
    button_text: str,
    wait_seconds: int = TELEGRAM_REPLY_TIMEOUT,
) -> str:
    """Python source that invokes one previously visible inline button.

    The runtime has already tied ``message_id`` and ``callback_data`` to a
    reply it observed in this run. The script re-reads that reply and verifies
    the button before submitting the callback to the same bot.
    """
    bot = "@" + bot_username.lstrip("@")
    sent = f"message_id={message_id} callback_data={callback_data}"
    return (
        "import base64\n"
        "import json\n"
        "import os\n"
        "import time\n"
        f"MAX_REPLIES = {MAX_REPLIES}\n"
        "from telethon.sync import TelegramClient\n"
        "from telethon.sessions import StringSession\n"
        "from telethon.tl.functions.messages import GetBotCallbackAnswerRequest\n"
        + _script_helpers()
        + "client = None\n"
        + "result = {\n"
        "    'action': 'callback',\n"
        f"    'attempted': {json.dumps(f'press {button_text}')},\n"
        f"    'sent': {json.dumps(sent)},\n"
        "    'delivered': False,\n"
        "    'replies': [],\n"
        "    'callback': None,\n"
        "    'post_press_message': None,\n"
        "    'error': None,\n"
        "}\n"
        "try:\n"
        "    client = TelegramClient(StringSession(os.environ['TELETHON_SESSION']), "
        "int(os.environ['TELETHON_API_ID']), os.environ['TELETHON_API_HASH'])\n"
        "    client.start()\n"
        f"    bot = client.get_entity({json.dumps(bot)})\n"
        f"    message = client.get_messages(bot, ids={message_id})\n"
        "    if message is None or message.out:\n"
        "        raise ValueError('the observed bot reply is no longer available')\n"
        f"    callback_data = {json.dumps(callback_data)}\n"
        "    markup = message_evidence(message)['reply_markup'] or {'buttons': []}\n"
        "    visible = {button['callback_data'] for button in markup['buttons']\n"
        "               if button['callback_data'] is not None}\n"
        "    if callback_data not in visible:\n"
        "        raise ValueError('the requested callback is not visible on that bot reply')\n"
        "    pre_press_message = message_evidence(message)\n"
        "    reply_baseline = newest_message_id(client, bot)\n"
        "    answer = client(GetBotCallbackAnswerRequest(\n"
        "        peer=bot, msg_id=message.id, data=base64.b64decode(callback_data)\n"
        "    ))\n"
        "    result['delivered'] = True\n"
        "    result['callback'] = {\n"
        "        'text': getattr(answer, 'message', None),\n"
        "        'alert': bool(getattr(answer, 'alert', False)),\n"
        "        'url': getattr(answer, 'url', None),\n"
        "    }\n"
        f"    deadline = time.monotonic() + {wait_seconds}\n"
        "    while time.monotonic() < deadline:\n"
        "        result['replies'] = received_replies(client, bot, reply_baseline)\n"
        "        post_press = client.get_messages(bot, ids=message.id)\n"
        "        result['post_press_message'] = (\n"
        "            message_evidence(post_press)\n"
        "            if post_press is not None and not post_press.out else None\n"
        "        )\n"
        "        if result['replies'] or result['post_press_message'] != pre_press_message:\n"
        "            break\n"
        "        time.sleep(1)\n"
        "except Exception as exc:\n"
        "    result['error'] = type(exc).__name__ + ': ' + str(exc)\n"
        "finally:\n"
        "    try:\n"
        "        if client is not None:\n"
        "            client.disconnect()\n"
        "    except Exception as exc:\n"
        "        if result['error'] is None:\n"
        "            result['error'] = type(exc).__name__ + ': ' + str(exc)\n"
        f"    print({json.dumps(PROBE_RESULT_MARKER)} + json.dumps(result))"
    )


def parse_bot_probe_result(stdout: str) -> dict:
    """Read the structured result the Telegram probe script printed.

    A probe that produced no marker produced no evidence, so it raises rather
    than passing an empty conversation off as "the bot stayed silent".
    """
    for line in stdout.splitlines():
        if line.startswith(PROBE_RESULT_MARKER):
            result = json.loads(line.removeprefix(PROBE_RESULT_MARKER))
            if not isinstance(result, dict):
                raise ValueError("the Telegram probe result is not an object")
            return result
    raise ValueError("the Telegram probe printed no result line")
