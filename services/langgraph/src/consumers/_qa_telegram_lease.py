"""One user of the shared QA Telegram identity at a time, held until its use has ended.

The QA account's Telethon session is used by native QA — the per-run identity
proof, the bot-access probe, the runtime's Telegram tools, the mechanical probe
and the exploratory executor's sandbox, which is served the session — and by the
synthetic buyer, which orders and probes a product as that same account. Two of
them talking as the account at once interleave in the same private chats, so a
reply one of them reads may be the other's. Observing that no QA run is queued
or running is not exclusion: a run admitted after the read overlaps whatever
follows it.

This is the exclusion, and it is the one authority over admission and release.

**The record.** One Redis hash per account, `LEASE_KEY`, with no TTL and an
explicit `state`: `idle`, `held` or `retained`. Admission takes only a record
that positively says `idle`. A missing record, an unknown state or a held record
without its token admits no one: the state of the identity is not known, so an
earlier holder may still be using it. Nothing turns absence into `idle` — not an
acquire, not elapsed time, not an empty read. Only `initialize`, run by an
operator who has proven every user stopped, writes the first `idle` (and the
one after a lost record). Release writes `idle` back, compared on the holder's
token; it never deletes the record, so a lost record stays lost until an
operator acts, even before any holder's watchdog notices.

**The account.** A hold owns the list of every lifetime that can use the
session: each Telegram client, probe child process, capability endpoint and
executor sandbox. A user registers its lifetime on the hold *before* the await
that could open it (connect, process start, endpoint start, executor creation)
and marks it ended only on positive proof — a disconnect that returned, a child
that exited, an endpoint that stopped, worker-manager's proven removal. Every
exit of the hold, cancellation included, settles that account once: all ended
releases to `idle`; anything open, or unknown, *retains* the record with the
names of what is outstanding, and no one is admitted until an operator who
checked them releases it by token. A cancelled or scheduled cleanup is not proof.

**Ownership loss.** A watchdog renews the hold; when the record stops naming it
(an operator release, or lost data) the holder's task is cancelled and the hold
ends in `IdentityOwnershipLost`. Because a lost record admits no one, loss never
lets a second user in beside the first, however late the watchdog fires.

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

#: The holder fields a release clears; the state itself is never deleted.
_HOLDER_FIELDS = "'token', 'kind', 'reference', 'purpose', 'acquired_at', 'renewed_at'"
_ACQUIRE = f"""
if redis.call('HGET', KEYS[1], 'state') ~= 'idle' then return 0 end
redis.call('HDEL', KEYS[1], 'retained', 'retained_at', 'released_at', 'released_by',
  {_HOLDER_FIELDS})
redis.call('HSET', KEYS[1], 'state', 'held', unpack(ARGV))
return 1
"""
_RENEW = """
if redis.call('HGET', KEYS[1], 'state') ~= 'held' then return 0 end
if redis.call('HGET', KEYS[1], 'token') ~= ARGV[1] then return 0 end
redis.call('HSET', KEYS[1], 'renewed_at', ARGV[2])
return 1
"""
_RETAIN = """
if redis.call('HGET', KEYS[1], 'state') ~= 'held' then return 0 end
if redis.call('HGET', KEYS[1], 'token') ~= ARGV[1] then return 0 end
redis.call('HSET', KEYS[1], 'state', 'retained', 'retained', ARGV[2], 'retained_at', ARGV[3])
return 1
"""
_RELEASE = f"""
local state = redis.call('HGET', KEYS[1], 'state')
if state ~= 'held' and not (ARGV[3] == 'operator' and state == 'retained') then return 0 end
if redis.call('HGET', KEYS[1], 'token') ~= ARGV[1] then return 0 end
redis.call('HDEL', KEYS[1], 'retained', 'retained_at', {_HOLDER_FIELDS})
redis.call('HSET', KEYS[1], 'state', 'idle', 'released_at', ARGV[2], 'released_by', ARGV[3])
return 1
"""
_INITIALIZE = """
local state = redis.call('HGET', KEYS[1], 'state')
local token = redis.call('HGET', KEYS[1], 'token')
if state == 'idle' then return 0 end
if (state == 'held' or state == 'retained') and token then return 0 end
redis.call('DEL', KEYS[1])
redis.call('HSET', KEYS[1], 'state', 'idle', 'initialized_at', ARGV[1])
return 1
"""


class LeaseState(StrEnum):
    IDLE = "idle"
    HELD = "held"
    RETAINED = "retained"


def lease_state(record: dict | None) -> LeaseState | None:
    """The record's state, or None when it is missing, unknown or malformed."""
    if not record:
        return None
    try:
        state = LeaseState(record.get("state"))
    except ValueError:
        return None
    if state is not LeaseState.IDLE and not record.get("token"):
        return None
    return state


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
    """The identity was not known idle for the whole bounded wait."""

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
    """An actionable, non-secret account of the identity's admission state."""
    state = lease_state(record)
    suffix = [] if waited is None else [f"waited {int(waited)}s"]
    if state is None:
        return "; ".join(
            [
                "the identity's admission state is missing or unknown, so an earlier user may "
                "still hold the session; nothing is admitted until an operator who has proven "
                "every user stopped initializes it (`identity --initialize`)",
                *suffix,
            ]
        )
    if state is LeaseState.IDLE:
        return "; ".join(["the shared QA Telegram identity is idle", *suffix])
    parts = [
        f"held by {record.get('kind')} {record.get('reference')} for {record.get('purpose')}",
        f"since {record.get('acquired_at')}",
        f"last renewed {record.get('renewed_at')}",
    ]
    moment = now or _utc_now()
    try:
        silent = (moment - datetime.fromisoformat(str(record.get("renewed_at")))).total_seconds()
    except (TypeError, ValueError):
        silent = None
    if state is LeaseState.RETAINED:
        parts.append(
            f"retained because {record.get('retained')}; verify that use has ended, then "
            f"release token {record.get('token')}"
        )
    elif silent is not None and silent >= STALE_AFTER_SECONDS:
        parts.append(
            f"not renewed for {int(silent)}s: the holder may be gone; verify its process and "
            f"executor are gone, then release token {record.get('token')}"
        )
    return "; ".join([*parts, *suffix])


@dataclass
class Lifetime:
    """One thing that can use the session: open until positively shown ended."""

    kind: str
    name: str
    ended: bool = False
    note: str | None = None

    def end(self) -> None:
        """Positive proof that this use is over: disconnected, exited, stopped, removed."""
        self.ended = True

    def unproven(self, note: str) -> None:
        """Why the end could not be shown. The lifetime stays open."""
        self.note = note

    def describe(self) -> str:
        return f"{self.kind} {self.name}" + (f" ({self.note})" if self.note else "")


def lifetime(hold: IdentityHold | None, kind: str, name: str) -> Lifetime:
    """Register a lifetime on *hold*; without a hold, a lifetime nobody settles."""
    return Lifetime(kind, name) if hold is None else hold.track(kind, name)


@dataclass
class IdentityHold:
    """One holder's use of the identity, from admission to its settled account."""

    holder: Holder
    token: str
    lost: bool = False
    #: Set at settlement: what was outstanding when the hold ended.
    retained: str | None = None
    released: bool = False
    lifetimes: list[Lifetime] = field(default_factory=list)
    _owner: asyncio.Task | None = field(default=None, repr=False)
    _cancelled_owner: bool = field(default=False, repr=False)

    def track(self, kind: str, name: str) -> Lifetime:
        """Register a use before the await that could open it."""
        used = Lifetime(kind, name)
        self.lifetimes.append(used)
        return used

    def outstanding(self) -> list[Lifetime]:
        return [used for used in self.lifetimes if not used.ended]


class TelegramIdentityLease:
    """The exclusive hold on one Telegram account's identity, kept in Redis."""

    def __init__(  # noqa: PLR0913 - every clock and wait is injectable
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
        """The current record, or None when there is none."""
        raw = await self._redis.hgetall(self.key)
        return {_text(key): _text(value) for key, value in raw.items()} or None

    def hold(self, holder: Holder, *, wait_seconds: float, poll_seconds: float) -> _HoldScope:
        """Admit *holder* once the identity is known idle, waiting at most *wait_seconds*."""
        return _HoldScope(self, holder, wait_seconds, poll_seconds)

    async def release(self, token: str) -> bool:
        """An operator's release of a held or retained record, by its exact token."""
        released = await self._script(_RELEASE, token, self._stamp(), "operator")
        logger.warning("qa_telegram_identity_released_by_operator", released=released)
        return released

    async def initialize(self) -> bool:
        """An operator's first `idle`, after proving every user stopped.

        Writes `idle` only over a missing, unknown or malformed record; never over a
        valid idle, held or retained one. Nothing else ever creates the record.
        """
        initialized = await self._script(_INITIALIZE, self._stamp())
        logger.warning("qa_telegram_identity_initialized_by_operator", initialized=initialized)
        return initialized

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
            except Exception as exc:  # noqa: BLE001 - Redis unreachable admits no one either
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
        """The one release decision: the account settled once, on every exit."""
        if held.lost:
            return
        outstanding = held.outstanding()
        if outstanding:
            held.retained = "outstanding: " + "; ".join(used.describe() for used in outstanding)
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
        held.released = await self._script(_RELEASE, held.token, self._stamp(), "holder")
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
    """`async with lease.hold(...) as held:` — admission, use, then the settled account."""

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
