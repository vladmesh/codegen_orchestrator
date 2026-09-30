#!/usr/bin/env python3
"""Send a native Telegram location to the bot under test and print its replies as JSON.

    qa probe telegram location /workspace/qa-library/telegram/location.py BOT LAT LON [WAIT]

BOT is the bot's @username, LAT and LON decimal degrees, WAIT the seconds to
collect replies (default 15, at most 40). Run `qa telegram_identity` first: the
location is sent as the proven QA account, through this run's proxy.

Exit status: 0 the location was delivered (an empty reply list is the bot's
silence, a result); 1 Telegram refused or failed; 2 an argument was refused
before anything was sent; 3 this run has no usable QA Telegram identity.

The coordinates are parsed with `float()`, must be finite and within
[-90, 90] / [-180, 180], and reach Telethon as values. This file generates no
source text. It runs inside the QA executor's sandbox, with Telethon and the
standard library only.
"""

import base64
import json
import math
import os
import re
import sys
import time

IDENTITY_FILE = "~/.qa/telegram_identity.json"
DEFAULT_WAIT_SECONDS = 15
MAX_WAIT_SECONDS = 40
POLL_INTERVAL = 2
MAX_REPLIES = 10
BOT_USERNAME = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")
USAGE = "usage: location.py BOT LAT LON [WAIT_SECONDS]"
REQUIRED_ARGUMENTS = 3
PROXY_FIELDS = 3  # ["http", host, port], as `qa telegram_identity` writes it

EXIT_TELEGRAM_FAILED = 1
EXIT_ARGUMENT_REFUSED = 2
EXIT_NO_IDENTITY = 3


class Refused(Exception):
    """This probe will not send anything, and says why."""

    def __init__(self, exit_status, message):
        super().__init__(message)
        self.exit_status = exit_status


def parse_coordinate(label, raw, bound):
    """A finite float within [-bound, bound], or a refusal naming the value."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise Refused(
            EXIT_ARGUMENT_REFUSED, f"{label} must be a decimal number, got {raw!r}"
        ) from None
    if not math.isfinite(value):
        raise Refused(EXIT_ARGUMENT_REFUSED, f"{label} must be finite, got {raw!r}")
    if not -bound <= value <= bound:
        raise Refused(
            EXIT_ARGUMENT_REFUSED, f"{label} must be within [-{bound}, {bound}], got {raw!r}"
        )
    return value


def parse_arguments(argv):
    """(bot, latitude, longitude, wait_seconds) from this probe's arguments."""
    if len(argv) not in (REQUIRED_ARGUMENTS, REQUIRED_ARGUMENTS + 1):
        raise Refused(EXIT_ARGUMENT_REFUSED, USAGE)
    match = BOT_USERNAME.match(argv[0])
    if match is None:
        raise Refused(EXIT_ARGUMENT_REFUSED, f"BOT must be a Telegram @username, got {argv[0]!r}")
    latitude = parse_coordinate("LAT", argv[1], 90)
    longitude = parse_coordinate("LON", argv[2], 180)
    wait_seconds = DEFAULT_WAIT_SECONDS
    if len(argv) > REQUIRED_ARGUMENTS:
        if not argv[3].isdigit() or int(argv[3]) > MAX_WAIT_SECONDS:
            raise Refused(
                EXIT_ARGUMENT_REFUSED,
                f"WAIT_SECONDS must be a whole number from 0 to {MAX_WAIT_SECONDS}, "
                f"got {argv[3]!r}",
            )
        wait_seconds = int(argv[3])
    return "@" + match.group(1), latitude, longitude, wait_seconds


def load_identity(path):
    """The proven QA identity `qa telegram_identity` wrote, or a refusal."""
    missing = (
        f"no QA Telegram identity at {path}: run `qa telegram_identity` first; "
        "if it refuses, this run has no proven QA Telegram identity"
    )
    try:
        with open(path, encoding="utf-8") as handle:
            identity = json.load(handle)
    except FileNotFoundError:
        raise Refused(EXIT_NO_IDENTITY, missing) from None
    except (OSError, ValueError) as exc:
        raise Refused(EXIT_NO_IDENTITY, f"the QA Telegram identity is unreadable: {exc}") from None
    if not isinstance(identity, dict):
        raise Refused(EXIT_NO_IDENTITY, "the QA Telegram identity is not an object")
    api_id = identity.get("api_id")
    if isinstance(api_id, bool) or not isinstance(api_id, int):
        raise Refused(EXIT_NO_IDENTITY, "the QA Telegram identity has no api_id")
    for field in ("api_hash", "session"):
        if not isinstance(identity.get(field), str) or not identity[field]:
            raise Refused(EXIT_NO_IDENTITY, f"the QA Telegram identity has no {field}")
    proxy = identity.get("proxy")
    if not (
        isinstance(proxy, list)
        and len(proxy) == PROXY_FIELDS
        and isinstance(proxy[1], str)
        and isinstance(proxy[2], int)
    ):
        raise Refused(
            EXIT_NO_IDENTITY,
            "the QA Telegram identity carries no proxy; this sandbox reaches Telegram "
            "only through the run's proxy",
        )
    return identity


def encode_callback_data(value):
    """Base64, as `qa telegram_click_button` takes it."""
    if value is None:
        return None
    return base64.b64encode(bytes(value)).decode("ascii")


def message_evidence(message):
    media = getattr(message, "media", None)
    media_type = type(media).__name__ if media is not None else None
    content = getattr(message, "raw_text", None)
    if content is None:
        content = getattr(message, "message", None)
    content = content or None
    is_attachment = media is not None and media_type != "MessageMediaWebPage"
    buttons = []
    markup = getattr(message, "reply_markup", None)
    for row_index, row in enumerate(getattr(markup, "rows", None) or []):
        for column_index, button in enumerate(getattr(row, "buttons", None) or []):
            buttons.append(
                {
                    "row": row_index,
                    "column": column_index,
                    "text": getattr(button, "text", None),
                    "type": type(button).__name__,
                    "callback_data": encode_callback_data(getattr(button, "data", None)),
                }
            )
    return {
        "id": message.id,
        "text": None if is_attachment else content,
        "caption": content if is_attachment else None,
        "media_type": media_type,
        "buttons": buttons,
    }


def received_replies(client, bot, minimum_id):
    found = client.get_messages(bot, min_id=minimum_id, limit=MAX_REPLIES)
    return [
        message_evidence(message)
        for message in reversed(list(found))
        if not message.out and message.id > minimum_id
    ]


def send_location(identity, bot, latitude, longitude, wait_seconds):
    """Send one location as the QA account and collect the bot's replies."""
    from telethon.sessions import StringSession
    from telethon.sync import TelegramClient
    from telethon.tl.types import InputGeoPoint, InputMediaGeoPoint

    result = {
        "action": "location",
        "bot": bot,
        "latitude": latitude,
        "longitude": longitude,
        "delivered": False,
        "sent_message_id": None,
        "replies": [],
        "error": None,
    }
    client = None
    try:
        client = TelegramClient(
            StringSession(identity["session"]),
            identity["api_id"],
            identity["api_hash"],
            proxy=tuple(identity["proxy"]),
        )
        client.connect()
        if not client.is_user_authorized():
            raise RuntimeError("the QA Telegram session is not authorized")
        entity = client.get_entity(bot)
        geo = InputMediaGeoPoint(geo_point=InputGeoPoint(lat=latitude, long=longitude))
        sent = client.send_file(entity, geo)
        result["delivered"] = True
        result["sent_message_id"] = sent.id
        deadline = time.monotonic() + wait_seconds
        while True:
            result["replies"] = received_replies(client, entity, sent.id)
            now = time.monotonic()
            if now >= deadline:
                break
            time.sleep(min(POLL_INTERVAL, deadline - now))
    except Exception as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        if client is not None:
            try:
                client.disconnect()
            except Exception as exc:
                if result["error"] is None:
                    result["error"] = type(exc).__name__ + ": " + str(exc)
    return result


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        bot, latitude, longitude, wait_seconds = parse_arguments(argv)
        identity = load_identity(os.path.expanduser(IDENTITY_FILE))
    except Refused as refused:
        sys.stderr.write(f"location probe refused: {refused}\n")
        return refused.exit_status
    result = send_location(identity, bot, latitude, longitude, wait_seconds)
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0 if result["delivered"] and result["error"] is None else EXIT_TELEGRAM_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
