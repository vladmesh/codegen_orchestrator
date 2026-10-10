"""How the buyer reads the live product's answers. Pure: texts in, judgements out."""

from __future__ import annotations

from collections.abc import Iterable
import re

from .telegram import Message

_CYRILLIC = re.compile(r"[а-яё]", re.I)
_LATIN = re.compile(r"[a-z]", re.I)
#: The core's fixed refusal while the product language is unset (kit CONTRACTS, binding v2).
LANGUAGE_UNSET = "Настройте язык продукта"
_DENIED = ("доступ запрещ", "access denied", "not authorized", "unauthorized", "forbidden")


def texts(messages: Iterable[Message]) -> list[str]:
    return [message.text for message in messages if message.text]


def access_denied(messages: Iterable[Message]) -> bool:
    return any(marker in text.casefold() for text in texts(messages) for marker in _DENIED)


def answered_in_russian(messages: list[Message]) -> bool:
    found = texts(messages)
    return (
        bool(found)
        and any(_CYRILLIC.search(text) for text in found)
        and not any(LANGUAGE_UNSET in text for text in found)
    )


def answered_in_english(messages: list[Message]) -> bool:
    found = texts(messages)
    return bool(found) and all(_LATIN.search(text) and not _CYRILLIC.search(text) for text in found)


def missing_channels(messages: list[Message], channels: list[str]) -> list[str]:
    said = " ".join(texts(messages)).casefold()
    return [name for name in channels if f"@{name}" not in said]


def channel_post_links(message: Message, channels: list[str]) -> list[tuple[str, int, str]]:
    """Links in *message* to a post of a configured channel: (channel, post id, url)."""
    wanted = {name.casefold() for name in channels}
    links = []
    for url in message.urls:
        match = re.fullmatch(r"https?://t\.me/([A-Za-z0-9_]+)/(\d+)/?", url)
        if match and match[1].casefold() in wanted:
            links.append((match[1].casefold(), int(match[2]), url))
    return links
