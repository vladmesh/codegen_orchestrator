"""How the buyer reads the live product's answers. Pure: texts in, judgements out."""

from __future__ import annotations

from collections.abc import Iterable
import re

from .telegram import Message

_CYRILLIC = re.compile(r"[а-яё]", re.I)
_LATIN = re.compile(r"[a-z]", re.I)
#: The core's fixed refusal while the product language is unset (kit CONTRACTS, binding v2).
LANGUAGE_UNSET = "Настройте язык продукта"
#: Product commands whose released answers carry no channel post: `/start` is the
#: core's help, `/channels` lists channel names (codegen-kit-tg-channels 0.1.2
#: `bindings/default.yaml`). `/digest` is the one command whose answer carries posts.
NON_SOURCE_COMMANDS = frozenset({"/start", "/channels"})
#: How the released `tg-channels.post` event renders a delivered post, by language
#: (codegen-kit-tg-channels 0.1.2 `bindings/default.yaml`, `events`): this prefix,
#: the channel, then the post's date, text and link on their own lines. A `/digest`
#: item renders the same post starting at the channel, without it.
POST_EVENT_PREFIX = {"ru": "Новая публикация: @", "en": "New post: @"}
_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
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


def event_channel(message: Message, language: str) -> str | None:
    """The channel a message in the post event's own form names, or None for any other."""
    prefix = POST_EVENT_PREFIX[language]
    if not message.text.startswith(prefix):
        return None
    name = message.text[len(prefix) :].split("\n", 1)[0].strip()
    return name.casefold() if _USERNAME.fullmatch(name) else None
