"""Durable ownership of direct PO replies sent to Telegram."""

import asyncio
import secrets
import time

import structlog

from shared.contracts.queues.po import (
    POPayloadProtectionError,
    POResponse,
    protect_po_payload,
    unprotect_po_payload,
)

from .proactive import send_text

logger = structlog.get_logger()

PENDING_KEY = "po:delivery:pending"
CLAIM_SECONDS = 300
SETTLED_SECONDS = 7 * 24 * 60 * 60
POLL_SECONDS = 0.5


class POResponsePending(TimeoutError):
    """The interactive deadline elapsed while final delivery remains owed."""

    def __init__(self, request_id: str):
        super().__init__("PO response remains pending")
        self.request_id = request_id


def _address_key(request_id: str) -> str:
    return f"po:delivery:address:{request_id}"


def _claim_key(request_id: str) -> str:
    return f"po:delivery:claim:{request_id}"


def _settled_key(request_id: str) -> str:
    return f"po:delivery:settled:{request_id}"


async def is_settled(redis, request_id: str) -> bool:
    """Report whether a final reply was already accepted by Telegram."""
    return bool(await redis.exists(_settled_key(request_id)))


def _response_stream(request_id: str) -> str:
    return f"po:response:{request_id}"


async def register_request(
    redis, request_id: str, telegram_chat_id: int, locale: str, wait_seconds: float
) -> None:
    """Record the protected recipient before publishing an accepted PO input."""
    key = _address_key(request_id)
    await redis.hset(
        key,
        mapping=protect_po_payload(
            key, {"telegram_chat_id": str(telegram_chat_id), "locale": locale}
        ),
    )
    await redis.zadd(PENDING_KEY, {request_id: time.time() + wait_seconds})


async def read_response(redis, request_id: str) -> POResponse | None:
    """Authenticate the first response from its released per-request stream."""
    stream = _response_stream(request_id)
    entries = await redis.xrange(stream, count=1)
    if not entries:
        return None
    logical = unprotect_po_payload(stream, entries[0][1])
    try:
        return POResponse.model_validate(logical)
    except Exception:
        raise POPayloadProtectionError("PO response validation failed") from None


async def deliver_response(redis, bot, request_id: str, response: POResponse) -> bool:
    """One Redis claim gates timely delivery, replay, and listener recovery."""
    if await redis.exists(_settled_key(request_id)):
        await _cleanup_settled(redis, request_id)
        return False
    token = secrets.token_hex(16)
    claim = _claim_key(request_id)
    if not await redis.set(claim, token, nx=True, ex=CLAIM_SECONDS):
        return False
    try:
        if await redis.exists(_settled_key(request_id)):
            return False
        address = await redis.hgetall(_address_key(request_id))
        locale = "en"
        if address:
            binding = unprotect_po_payload(_address_key(request_id), address)
            expected = binding["telegram_chat_id"]
            if expected != response.telegram_chat_id:
                raise POPayloadProtectionError("PO response recipient mismatch")
            locale = binding["locale"]
        recipient = int(response.telegram_chat_id)
        if response.error == "true" or not response.text:
            from .texts import text

            delivery_text = text(locale, "message_failed")
        else:
            delivery_text = response.text
        async with asyncio.timeout(CLAIM_SECONDS - 30):
            await send_text(bot, recipient, delivery_text)
        await redis.set(_settled_key(request_id), "1", ex=SETTLED_SECONDS)
        await _cleanup_settled(redis, request_id)
        logger.info("po_response_delivered", request_id=request_id, telegram_chat_id=recipient)
        return True
    finally:
        held = await redis.get(claim)
        if held in (token, token.encode()):
            await redis.delete(claim)


async def _cleanup_settled(redis, request_id: str) -> None:
    """Retry stream and index cleanup while the settlement marker is retained."""
    try:
        await redis.zrem(PENDING_KEY, request_id)
        await redis.delete(_address_key(request_id), _response_stream(request_id))
    except Exception as exc:
        logger.warning("po_response_cleanup_failed", error_type=type(exc).__name__)


class POResponseListener:
    """Recover accepted replies after the interactive handler exits or the bot restarts."""

    def __init__(self, client):
        self.client = client

    async def _attempt(self, bot, request_id: str) -> None:
        try:
            response = await read_response(self.client.redis, request_id)
            if response is not None:
                await deliver_response(self.client.redis, bot, request_id, response)
        except Exception as exc:
            logger.error(
                "po_response_delivery_failed",
                error_type=type(exc).__name__,
            )

    async def poll_once(self, bot) -> None:
        """Read every owed request and recover released response streams."""
        redis = self.client.redis
        request_ids = await redis.zrangebyscore(PENDING_KEY, "-inf", time.time())
        for raw_id in request_ids:
            request_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
            await self._attempt(bot, request_id)
        # Released response streams have no delivery index. Preserve and
        # settle them through the same claim during a rolling upgrade.
        async for key in redis.scan_iter(match="po:response:*"):
            stream = key.decode() if isinstance(key, bytes) else key
            if stream.endswith(":dlq"):
                continue
            request_id = stream.removeprefix("po:response:")
            if await redis.zscore(PENDING_KEY, request_id) is not None:
                continue
            await self._attempt(bot, request_id)

    async def run(self, bot) -> None:
        """Keep recovery active for the lifetime of this bot process."""
        while True:
            try:
                await self.poll_once(bot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("po_response_listener_failed", error_type=type(exc).__name__)
            await asyncio.sleep(POLL_SECONDS)
