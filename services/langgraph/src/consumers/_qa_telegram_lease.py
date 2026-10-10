"""One user of the shared QA Telegram identity at a time, held until its use has ended.

The QA account's Telethon session is used by native QA — the per-run identity
proof, the bot-access probe, the runtime's Telegram tools, the mechanical probe
and the exploratory executor's sandbox, which is served the session — and by the
synthetic buyer, which orders and probes a product as that same account. Two of
them talking as the account at once interleave in the same private chats, so a
reply one of them reads may be the other's. Observing that no QA run is queued
or running is not exclusion: a run admitted after the read overlaps whatever
follows it.

This is the exclusion. Every user takes a `TelegramIdentityLease.hold` before it
proves, connects or hands out the identity, and the hold is released only when
the holder says its use has ended: its client disconnected, and every sandbox it
served the session to removed. A holder that cannot show that — a disconnect that
failed, an executor whose removal was not confirmed — *retains* the lease: it
stays held, names why, and no one else is admitted until an operator who has
checked the named holder releases it by its token. Time alone never releases a
lease. The record has no TTL, because a key that expires says nothing about
whether a client or a sandbox stopped using the session.

The record is one Redis hash, written and changed only by compare-and-act
scripts keyed on the holder's token, so a holder can never release, renew or
retain another holder's lease. A watchdog renews the hold while it is in use;
when the record stops naming its token (an operator released it, or the data
was lost) the holder's task is cancelled, its own cleanup runs, and the hold
ends in `IdentityOwnershipLost` rather than carrying on beside a new holder.

Native QA reaches this through the QA consumer; the synthetic buyer through its
controller. Both hold the same key, `LEASE_KEY` of the Telegram user id.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
import time
from typing import Any
import uuid

import structlog

logger = structlog.get_logger(__name__)

LEASE_KEY = "qa:telegram-identity:{telegram_id}"
#: How often a holder confirms the record still names it.
RENEW_SECONDS = 15
#: A record not renewed for this long is reported as possibly orphaned. Reported,
#: never released: only the holder, or an operator naming its token, releases it.
STALE_AFTER_SECONDS = 120

_ACQUIRE = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
redis.call('HSET', KEYS[1], unpack(ARGV))
return 1
"""
_RENEW = """
if redis.call('HGET', KEYS[1], 'token') ~= ARGV[1] then return 0 end
redis.call('HSET', KEYS[1], 'renewed_at', ARGV[2])
return 1
"""
_RETAIN = """
if redis.call('HGET', KEYS[1], 'token') ~= ARGV[1] then return 0 end
redis.call('HSET', KEYS[1], 'retained', ARGV[2], 'retained_at', ARGV[3])
return 1
"""
_RELEASE = """
if redis.call('HGET', KEYS[1], 'token') ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1])
return 1
"""


class HolderKind(StrEnum):
    NATIVE_QA = "native_qa"
    SYNTHETIC_BUYER = "synthetic_buyer"


@dataclass(frozen=True)
class Holder:
    """Who holds the identity and for what. Never a credential."""

    kind: HolderKind
    #: The QA run id, or the synthetic-buyer operation id.
    reference: str
    #: What the hold is for: `exploratory`, `mechanical`, `buyer:order`, ...
    purpose: str


class IdentityBusy(Exception):  # noqa: N818 - a refusal named by its cause
    """Another holder kept the identity for the whole bounded wait."""

    def __init__(self, record: dict | None, waited: float, detail: str) -> None:
        self.record = record
        self.waited = waited
        super().__init__(detail)


class IdentityOwnershipLost(Exception):  # noqa: N818
    """The record stopped naming this hold while it was in use; its use was stopped."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def describe(
    record: dict | None, *, waited: float | None = None, now: datetime | None = None
) -> str:
    """An actionable, non-secret account of who holds the identity."""
    if not record:
        return "the shared QA Telegram identity is free"
    parts = [
        f"held by {record.get('kind')} {record.get('reference')} for {record.get('purpose')}",
        f"since {record.get('acquired_at')}",
        f"last renewed {record.get('renewed_at')}",
    ]
    renewed = record.get("renewed_at")
    moment = now or datetime.now(UTC)
    try:
        silent = (moment - datetime.fromisoformat(str(renewed))).total_seconds()
    except (TypeError, ValueError):
        silent = None
    if record.get("retained"):
        parts.append(
            f"retained because {record['retained']}; verify that use has ended, then release "
            f"token {record.get('token')}"
        )
    elif silent is not None and silent >= STALE_AFTER_SECONDS:
        parts.append(
            f"not renewed for {int(silent)}s: the holder may be gone; verify its process and "
            f"executor are gone, then release token {record.get('token')}"
        )
    if waited is not None:
        parts.append(f"waited {int(waited)}s")
    return "; ".join(parts)


@dataclass
class IdentityHold:
    """One holder's use of the identity, from admission to proven end."""

    holder: Holder
    token: str
    lost: bool = False
    retained: str | None = None
    released: bool = False
    _owner: asyncio.Task | None = field(default=None, repr=False)
    _cancelled_owner: bool = field(default=False, repr=False)

    def retain(self, reason: str) -> None:
        """Use could not be shown to have ended: keep the lease when this hold exits."""
        if self.retained is None:
            self.retained = reason


class TelegramIdentityLease:
    """The exclusive hold on one Telegram account's identity, kept in Redis."""

    def __init__(
        self,
        redis: Any,
        telegram_id: int,
        *,
        wall: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        renew_wait: Callable[[], Awaitable[None]] | None = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._redis = redis
        self.telegram_id = telegram_id
        self.key = LEASE_KEY.format(telegram_id=telegram_id)
        self._wall = wall or time.monotonic
        self._sleep = sleep
        self._renew_wait = renew_wait or (lambda: asyncio.sleep(RENEW_SECONDS))
        self._now = now

    def _stamp(self) -> str:
        return self._now().isoformat()

    def describe(self, record: dict | None, *, waited: float | None = None) -> str:
        return describe(record, waited=waited, now=self._now())

    async def _script(self, script: str, *args: str) -> bool:
        return int(await self._redis.eval(script, 1, self.key, *args)) == 1

    async def holder(self) -> dict | None:
        """The current record, or None when the identity is free."""
        raw = await self._redis.hgetall(self.key)
        return {_text(key): _text(value) for key, value in raw.items()} or None

    def hold(self, holder: Holder, *, wait_seconds: float, poll_seconds: float) -> _HoldScope:
        """Admit *holder* once the identity is free, waiting at most *wait_seconds*."""
        return _HoldScope(self, holder, wait_seconds, poll_seconds)

    async def release(self, token: str) -> bool:
        """An operator's release of a retained or orphaned lease, by its exact token."""
        released = await self._script(_RELEASE, token)
        logger.warning("qa_telegram_identity_released_by_operator", released=released)
        return released

    # --- one hold ---------------------------------------------------------------

    async def _acquire(self, holder: Holder, wait_seconds: float, poll_seconds: float) -> str:
        token = uuid.uuid4().hex
        started = self._wall()
        while True:
            stamp = self._stamp()
            fields = {
                "token": token,
                "kind": holder.kind.value,
                "reference": holder.reference,
                "purpose": holder.purpose,
                "acquired_at": stamp,
                "renewed_at": stamp,
            }
            flat = [part for pair in fields.items() for part in pair]
            if await self._script(_ACQUIRE, *flat):
                logger.info(
                    "qa_telegram_identity_acquired",
                    kind=holder.kind.value,
                    reference=holder.reference,
                    purpose=holder.purpose,
                )
                return token
            waited = self._wall() - started
            if waited >= wait_seconds:
                record = await self.holder()
                detail = self.describe(record, waited=waited)
                logger.warning(
                    "qa_telegram_identity_busy",
                    kind=holder.kind.value,
                    reference=holder.reference,
                    detail=detail,
                )
                raise IdentityBusy(record, waited, detail)
            await self._sleep(poll_seconds)

    async def _watch(self, held: IdentityHold) -> None:
        """Renew while in use; stop the holder's task the moment the record is not its own."""
        while True:
            await self._renew_wait()
            try:
                renewed = await self._script(_RENEW, held.token, self._stamp())
            except Exception as exc:  # noqa: BLE001 - Redis unreachable is not ownership lost
                logger.warning("qa_telegram_identity_renew_failed", error=type(exc).__name__)
                continue
            if not renewed:
                held.lost = True
                logger.error(
                    "qa_telegram_identity_ownership_lost",
                    kind=held.holder.kind.value,
                    reference=held.holder.reference,
                )
                if held._owner is not None:
                    held._cancelled_owner = True
                    held._owner.cancel(msg="qa_telegram_identity_ownership_lost")
                return

    async def _settle(self, held: IdentityHold) -> None:
        if held.lost:
            return
        if held.retained is not None:
            kept = await self._script(_RETAIN, held.token, held.retained, self._stamp())
            logger.error(
                "qa_telegram_identity_retained",
                kind=held.holder.kind.value,
                reference=held.holder.reference,
                reason=held.retained,
                token=held.token,
                recorded=kept,
            )
            if not kept:
                held.lost = True
            return
        held.released = await self._script(_RELEASE, held.token)
        if not held.released:
            held.lost = True
            logger.error(
                "qa_telegram_identity_ownership_lost",
                kind=held.holder.kind.value,
                reference=held.holder.reference,
            )
            return
        logger.info(
            "qa_telegram_identity_released",
            kind=held.holder.kind.value,
            reference=held.holder.reference,
        )


class _HoldScope:
    """`async with lease.hold(...) as held:` — admission, use, then release or retention."""

    def __init__(
        self, lease: TelegramIdentityLease, holder: Holder, wait_seconds: float, poll: float
    ) -> None:
        self._lease = lease
        self._holder = holder
        self._wait = wait_seconds
        self._poll = poll
        self._held: IdentityHold | None = None
        self._watchdog: asyncio.Task | None = None

    async def __aenter__(self) -> IdentityHold:
        token = await self._lease._acquire(self._holder, self._wait, self._poll)
        held = IdentityHold(self._holder, token, _owner=asyncio.current_task())
        self._held = held
        try:
            self._watchdog = asyncio.create_task(self._lease._watch(held))
        except BaseException:
            await self._lease._settle(held)
            raise
        return held

    async def __aexit__(self, kind, error, trace) -> bool:
        held = self._held
        assert held is not None  # noqa: S101 - entered before exited
        if self._watchdog is not None:
            self._watchdog.cancel()
            with suppress(asyncio.CancelledError):
                await self._watchdog
        if held._cancelled_owner and held._owner is not None:
            # The cancellation this hold caused is ownership loss, not the caller's
            # cancel: take it back, delivered or still pending, and say what it was.
            if not isinstance(error, asyncio.CancelledError):
                with suppress(asyncio.CancelledError):
                    await asyncio.sleep(0)
            held._owner.uncancel()
        await asyncio.shield(self._lease._settle(held))
        if held.lost:
            raise IdentityOwnershipLost(
                f"the QA Telegram identity hold of {held.holder.kind.value} "
                f"{held.holder.reference} was lost while in use"
            ) from None
        return False
