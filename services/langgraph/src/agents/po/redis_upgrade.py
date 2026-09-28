"""Count/convert released PO Redis payloads with every producer and consumer stopped."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import os
from uuid import uuid4

from redis.asyncio import Redis
import structlog

from shared.contracts.queues.po import (
    PO_PAYLOAD_ENVELOPE,
    POReminderMessage,
    POSystemEvent,
    protect_po_payload,
    unprotect_po_payload,
)
from shared.log_config import setup_logging
from shared.queues import (
    PO_CONSUMER_GROUP,
    PO_INPUT_QUEUE,
    PO_PROACTIVE_GROUP,
    PO_PROACTIVE_QUEUE,
)
from shared.redis.po import LATEST_OWNER_EVENT_KEY_PREFIX, po_payload_keys

STREAM_GROUPS = {PO_INPUT_QUEUE: PO_CONSUMER_GROUP, PO_PROACTIVE_QUEUE: PO_PROACTIVE_GROUP}
DLQ_FIELDS = {
    "source_stream",
    "group",
    "entry_id",
    "failure",
    "reason",
    "quarantined_at",
    "body",
}
CATEGORIES = ("input", "proactive", "response", "dlq", "reminders", "owner_events")
MIN_REDIS_VERSION = 7


class PORedisUpgradeError(RuntimeError):
    """Count-only refusal; no Redis values or chained exceptions."""


@dataclass
class PayloadKey:
    key: str
    category: str
    entries: list
    expiry: int
    info: dict | None = None
    groups: list | None = None
    consumers: dict | None = None
    plaintext_count: int = 0


def _id(value: str) -> tuple[int, int]:
    return tuple(int(part) for part in value.split("-"))


async def _read(redis, key: str, category: str) -> PayloadKey:
    expected = (
        "zset" if category == "reminders" else "string" if category == "owner_events" else "stream"
    )
    if await redis.type(key) != expected:
        raise ValueError
    expiry = await redis.pexpiretime(key)
    if category == "reminders":
        return PayloadKey(key, category, await redis.zrange(key, 0, -1, withscores=True), expiry)
    if category == "owner_events":
        return PayloadKey(key, category, [(await redis.get(key), None)], expiry)
    info = await redis.xinfo_stream(key)
    groups = await redis.xinfo_groups(key)
    expected_group = STREAM_GROUPS.get(key)
    if expected_group:
        if (info["length"] and not groups) or any(g["name"] != expected_group for g in groups):
            raise ValueError
    elif groups:
        # Released direct responses and DLQs have no group/recovery state.
        raise ValueError
    consumers = {}
    for group in groups:
        if group["pending"] or _id(group["last-delivered-id"]) < _id(info["last-generated-id"]):
            raise ValueError
        if group["lag"] not in (0, None):
            raise ValueError
        consumers[group["name"]] = await redis.xinfo_consumers(key, group["name"])
    return PayloadKey(key, category, await redis.xrange(key), expiry, info, groups, consumers)


def _protect_record(key: str, category: str, raw: dict) -> tuple[dict, bool]:
    if PO_PAYLOAD_ENVELOPE in raw:
        logical = unprotect_po_payload(key, raw)
        protected = raw
        plaintext = False
    else:
        logical = raw
        protected = protect_po_payload(key, raw)
        plaintext = True
    if category == "reminders":
        if set(logical) - set(POReminderMessage.model_fields):
            raise ValueError
        POReminderMessage.model_validate(logical)
    elif category == "owner_events":
        if set(logical) - set(POSystemEvent.model_fields):
            raise ValueError
        event = POSystemEvent.model_validate(logical)
        if (
            not event.story_id
            or key != f"{LATEST_OWNER_EVENT_KEY_PREFIX}{event.telegram_chat_id}:{event.story_id}"
        ):
            raise ValueError
    elif category == "dlq":
        if set(logical) != DLQ_FIELDS or logical["failure"] not in (
            "decode_error",
            "validation_error",
        ):
            raise ValueError
        if key != f"{logical['source_stream']}:dlq":
            raise ValueError
        if not isinstance(json.loads(logical["body"]), dict):
            raise ValueError
        json.loads(logical["reason"])
    elif plaintext and not all(
        isinstance(k, str) and isinstance(v, str) for k, v in logical.items()
    ):
        # Released streams are flat maps, including retained poison evidence.
        raise ValueError
    return protected, plaintext


async def _stage(redis, record: PayloadKey, staged_key: str) -> None:
    if record.category == "owner_events":
        await redis.set(staged_key, record.entries[0][0])
    elif record.category == "reminders":
        await redis.zadd(staged_key, dict(record.entries))
    else:
        # An empty retained stream is still a stream, with cursor/counter state.
        await redis.xgroup_create(staged_key, "po-upgrade", id="0", mkstream=True)
        await redis.xgroup_destroy(staged_key, "po-upgrade")
        for entry_id, fields in record.entries:
            await redis.xadd(staged_key, fields, id=entry_id)
        await redis.execute_command(
            "XSETID",
            staged_key,
            record.info["last-generated-id"],
            "ENTRIESADDED",
            record.info["entries-added"],
            "MAXDELETEDID",
            record.info["max-deleted-entry-id"],
        )
        for group in record.groups:
            await redis.xgroup_create(
                staged_key,
                group["name"],
                id=group["last-delivered-id"],
                entries_read=group["entries-read"],
            )
            for consumer in record.consumers[group["name"]]:
                await redis.xgroup_createconsumer(staged_key, group["name"], consumer["name"])


async def _prepare_records(redis, keys):
    before = {category: {"plaintext": 0, "protected": 0} for category in CATEGORIES}
    records = []
    invalid_keys = 0
    for key, category in keys.items():
        try:
            record = await _read(redis, key, category)
            entries = []
            for first, second in record.entries:
                stream = record.info is not None
                raw = second if stream else json.loads(first)
                protected, plaintext = _protect_record(key, category, raw)
                before[category]["plaintext" if plaintext else "protected"] += 1
                record.plaintext_count += int(plaintext)
                entries.append((first, protected) if stream else (json.dumps(protected), second))
            record.entries = entries
            records.append(record)
        except Exception:
            invalid_keys += 1
    if invalid_keys:
        raise PORedisUpgradeError(
            f"PO Redis upgrade refused: invalid_or_undrained_keys={invalid_keys}; changed=0"
        )
    return before, records


async def _replace_records(redis, transaction, records, keys, temporary):
    changed = [record for record in records if record.plaintext_count]
    for record in changed:
        staged_key = f"po-upgrade-staging:{uuid4()}"
        temporary.append(staged_key)
        await _stage(redis, record, staged_key)
        await transaction.watch(staged_key)
        # Readback authenticates every staged payload before switching any key.
        if record.info is not None:
            values = [v for _, v in await redis.xrange(staged_key)]
        elif record.category == "reminders":
            values = [json.loads(v) for v in await redis.zrange(staged_key, 0, -1)]
        else:
            values = [json.loads(await redis.get(staged_key))]
        if len(values) != len(record.entries):
            raise ValueError
        for value in values:
            unprotect_po_payload(record.key, value)
    if await po_payload_keys(redis) != keys:
        raise ValueError
    transaction.multi()
    for record, staged_key in zip(changed, temporary, strict=True):
        transaction.rename(staged_key, record.key)
        if record.expiry >= 0:
            transaction.pexpireat(record.key, record.expiry)
    await transaction.execute()


async def upgrade(redis, *, writers_quiesced: bool, apply: bool = False) -> dict:
    if not writers_quiesced:
        raise PORedisUpgradeError("PO Redis upgrade requires quiesced producers and consumers")
    # Validate configuration even when Redis has no payload keys.
    try:
        probe = protect_po_payload("po-upgrade-probe", {})
        unprotect_po_payload("po-upgrade-probe", probe)
        if int((await redis.info("server"))["redis_version"].split(".")[0]) < MIN_REDIS_VERSION:
            raise ValueError
    except Exception:
        raise PORedisUpgradeError(
            "PO Redis upgrade preflight failed; check key and Redis >= 7"
        ) from None
    temporary = []
    try:
        keys = await po_payload_keys(redis)
        async with redis.pipeline(transaction=True) as transaction:
            if keys:
                await transaction.watch(*keys)
            before, records = await _prepare_records(redis, keys)
            if apply:
                await _replace_records(redis, transaction, records, keys, temporary)
        after = (
            (await _prepare_records(redis, await po_payload_keys(redis)))[0] if apply else before
        )
        if apply and any(counts["plaintext"] for counts in after.values()):
            raise PORedisUpgradeError("PO Redis upgrade readback failed: plaintext remains")
        return {
            "mode": "apply" if apply else "dry-run",
            "before": before,
            "converted" if apply else "would_convert": {
                category: counts["plaintext"] for category, counts in before.items()
            },
            "after": after,
        }
    except PORedisUpgradeError:
        raise
    except Exception:
        raise PORedisUpgradeError(
            "PO Redis upgrade failed; keep writers stopped and run count-only readback"
        ) from None
    finally:
        if temporary:
            await redis.unlink(*temporary)


async def _main(args) -> int:
    logger = structlog.get_logger(__name__)
    url = os.environ.get("REDIS_URL")
    if not url:
        logger.error("po_redis_upgrade_failed", reason="REDIS_URL is required")
        return 1
    async with Redis.from_url(url, decode_responses=True) as redis:
        try:
            report = await upgrade(redis, writers_quiesced=args.writers_quiesced, apply=args.apply)
        except PORedisUpgradeError as exc:
            logger.error("po_redis_upgrade_failed", reason=str(exc))
            return 1
    logger.info("po_redis_upgrade_counts", **report)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--writers-quiesced", action="store_true", required=True)
    parser.add_argument("--apply", action="store_true")
    setup_logging(service_name="po-redis-upgrade", log_format="json")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
