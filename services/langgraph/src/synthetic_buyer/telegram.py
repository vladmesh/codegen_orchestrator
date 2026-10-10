"""The buyer's one Telegram session, as the controller needs it.

The controller talks to a `TelegramPort`; the live adapter is Telethon over the
production QA account's session. The session is shared with the QA runtime, so
the controller connects it only for the customer conversation and for product
probes, and disconnects it before every wait on native work.

Every Telethon call is bounded and a failure keeps only the exception's class
name: Telethon's text may quote what it was handed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
import re
from typing import Any, Protocol

from .config import TelegramCredentials, resolve_secret
from .evidence import Redaction

CALL_TIMEOUT_SECONDS = 30
#: Messages read per page above a watermark; a busier dialog reads again.
PAGE = 50
_URL = re.compile(r"https?://\S+")


class TransportError(RuntimeError):
    """One Telegram call failed. `stage` names it; no Telethon text is kept."""

    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail


@dataclass(frozen=True)
class Button:
    text: str
    data: bytes | None = None
    url: str | None = None


@dataclass(frozen=True)
class Message:
    """One message of a dialog, reduced to what the controller reads."""

    id: int
    sender_id: int | None
    outgoing: bool
    date: datetime
    text: str
    buttons: tuple[Button, ...] = ()
    urls: tuple[str, ...] = ()
    reply_to: int | None = None
    #: The channel a forwarded post came from, when Telegram says so.
    forwarded_from_channel: int | None = None
    edited: bool = False

    def evidence(self, redaction: Redaction) -> dict:
        return {
            "id": self.id,
            "sender_id": self.sender_id,
            "outgoing": self.outgoing,
            "date": self.date.isoformat(),
            "text": redaction.text(self.text),
            "buttons": [redaction.text(button.text) for button in self.buttons],
            "urls": [redaction.text(url) for url in self.urls],
            "reply_to": self.reply_to,
            "forwarded_from_channel": self.forwarded_from_channel,
        }


@dataclass(frozen=True)
class Peer:
    id: int
    username: str
    is_bot: bool


class TelegramPort(Protocol):
    """What the controller does with the buyer's session."""

    @property
    def connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def me(self) -> int: ...

    async def resolve(self, username: str) -> Peer: ...

    async def latest_id(self, peer: Peer) -> int: ...

    async def messages_after(self, peer: Peer, after_id: int) -> list[Message]: ...

    async def send(self, peer: Peer, text: str) -> Message: ...

    async def press(self, peer: Peer, message_id: int, data: bytes) -> None: ...

    async def post_date(self, channel: str, post_id: int) -> datetime | None: ...


@dataclass
class TelethonPort:
    """The live adapter: the production QA account's Telethon session.

    Credentials are resolved from their handles only in `connect`, added to the
    operation's redaction set at once, and handed to Telethon and nothing else.
    """

    credentials: TelegramCredentials
    environ: Mapping[str, str]
    redaction: Redaction
    timeout: float = CALL_TIMEOUT_SECONDS
    _client: Any = field(default=None, init=False, repr=False)

    @property
    def connected(self) -> bool:
        """Whether a client exists that may hold a connection: from its creation until
        a disconnect returned, setup failures and cancellations included."""
        return self._client is not None

    async def _call(self, awaitable: Awaitable[Any], stage: str) -> Any:
        try:
            return await asyncio.wait_for(awaitable, timeout=self.timeout)
        except TimeoutError:
            raise TransportError(stage, f"did not answer in {self.timeout}s") from None
        except Exception as exc:  # noqa: BLE001 - every Telethon failure is this stage's
            raise TransportError(stage, f"failed: {type(exc).__name__}") from None

    async def connect(self) -> None:
        from telethon import TelegramClient  # noqa: PLC0415 - live mode only
        from telethon.sessions import StringSession  # noqa: PLC0415

        if self._client is not None:
            return
        session = resolve_secret(self.credentials.session, self.environ)
        api_hash = resolve_secret(self.credentials.api_hash, self.environ)
        api_id = resolve_secret(self.credentials.api_id, self.environ)
        self.redaction.add(session, api_hash)
        if not api_id.isdecimal():
            raise TransportError("connect", f"{self.credentials.api_id.describe()} is not numeric")
        try:
            client = TelegramClient(
                StringSession(session), int(api_id), api_hash, receive_updates=False
            )
        except Exception as exc:  # noqa: BLE001 - a session string Telethon cannot load
            raise TransportError("connect", f"session refused: {type(exc).__name__}") from None
        # Kept from here, before any await could open a connection: whatever fails
        # in setup, the caller's disconnect still reaches this client.
        self._client = client
        await self._call(client.connect(), "connect")
        if not await self._call(client.is_user_authorized(), "authorization"):
            raise TransportError("authorization", "the session is not authorized")

    async def disconnect(self) -> None:
        """Disconnect; the client is dropped only once its disconnect has returned."""
        client = self._client
        if client is not None:
            await self._call(client.disconnect(), "disconnect")
            self._client = None

    def _require(self) -> Any:
        if self._client is None:
            raise TransportError("session", "the session is not connected")
        return self._client

    async def me(self) -> int:
        user = await self._call(self._require().get_me(), "get_me")
        if user is None:
            raise TransportError("get_me", "returned no user")
        return int(user.id)

    async def resolve(self, username: str) -> Peer:
        entity = await self._call(self._require().get_entity(f"@{username}"), "resolve")
        return Peer(
            id=int(entity.id),
            username=str(getattr(entity, "username", "") or ""),
            is_bot=bool(getattr(entity, "bot", False)),
        )

    async def latest_id(self, peer: Peer) -> int:
        found = await self._call(self._require().get_messages(peer.id, limit=1), "read")
        return found[0].id if found else 0

    async def messages_after(self, peer: Peer, after_id: int) -> list[Message]:
        found = await self._call(
            self._require().get_messages(peer.id, min_id=after_id, limit=PAGE), "read"
        )
        return sorted(
            (to_message(item) for item in found if item.id > after_id), key=lambda m: m.id
        )

    async def send(self, peer: Peer, text: str) -> Message:
        sent = await self._call(self._require().send_message(peer.id, text), "send")
        return to_message(sent)

    async def press(self, peer: Peer, message_id: int, data: bytes) -> None:
        from telethon.tl.functions.messages import (  # noqa: PLC0415 - live mode only
            GetBotCallbackAnswerRequest,
        )

        await self._call(
            self._require()(
                GetBotCallbackAnswerRequest(peer=peer.id, msg_id=message_id, data=data)
            ),
            "press",
        )

    async def post_date(self, channel: str, post_id: int) -> datetime | None:
        """When a public channel published *post_id*, read from the channel itself."""
        post = await self._call(
            self._require().get_messages(f"@{channel}", ids=post_id), "read channel post"
        )
        return None if post is None else post.date


def to_message(item: Any) -> Message:
    """A Telethon message reduced to the controller's view of it."""
    text = getattr(item, "raw_text", None) or getattr(item, "message", None) or ""
    buttons: list[Button] = []
    markup = getattr(item, "reply_markup", None)
    for row in getattr(markup, "rows", None) or []:
        for button in getattr(row, "buttons", None) or []:
            buttons.append(
                Button(
                    text=str(getattr(button, "text", "")),
                    data=getattr(button, "data", None),
                    url=getattr(button, "url", None),
                )
            )
    urls = [match.group(0) for match in _URL.finditer(text)]
    for entity in getattr(item, "entities", None) or []:
        url = getattr(entity, "url", None)
        if url:
            urls.append(str(url))
    forwarded = getattr(item, "fwd_from", None)
    origin = getattr(forwarded, "from_id", None) if forwarded is not None else None
    reply = getattr(item, "reply_to", None)
    return Message(
        id=int(item.id),
        sender_id=getattr(item, "sender_id", None),
        outgoing=bool(getattr(item, "out", False)),
        date=item.date,
        text=str(text),
        buttons=tuple(buttons),
        urls=tuple(dict.fromkeys(urls)),
        reply_to=getattr(reply, "reply_to_msg_id", None),
        forwarded_from_channel=getattr(origin, "channel_id", None),
        edited=getattr(item, "edit_date", None) is not None,
    )
