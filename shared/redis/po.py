"""PO-only payload inventory and fail-closed startup readback."""

import json

from shared.contracts.queues.po import (
    POPayloadProtectionError,
    protect_po_payload,
    unprotect_po_payload,
)
from shared.queues import PO_INPUT_QUEUE, PO_PROACTIVE_QUEUE, PO_REMINDERS_KEY

LATEST_OWNER_EVENT_KEY_PREFIX = "po:latest_owner_event:"


async def po_payload_keys(redis) -> dict[str, str]:
    keys = {}
    for key, category in (
        (PO_INPUT_QUEUE, "input"),
        (PO_PROACTIVE_QUEUE, "proactive"),
        (PO_REMINDERS_KEY, "reminders"),
    ):
        if await redis.exists(key):
            keys[key] = category
    for pattern, category in (
        ("po:response:*", "response"),
        ("po:input:dlq", "dlq"),
        ("po:proactive:dlq", "dlq"),
        ("po:response:*:dlq", "dlq"),
        (f"{LATEST_OWNER_EVENT_KEY_PREFIX}*", "owner_events"),
    ):
        async for key in redis.scan_iter(match=pattern):
            keys[key] = category
    return keys


async def verify_po_storage(redis) -> None:
    """Refuse released plaintext before any runtime consumer can ACK or move it."""
    invalid = 0
    try:
        unprotect_po_payload("po-startup-probe", protect_po_payload("po-startup-probe", {}))
        for key, category in (await po_payload_keys(redis)).items():
            try:
                if category == "reminders":
                    values = [json.loads(v) for v in await redis.zrange(key, 0, -1)]
                elif category == "owner_events":
                    raw = await redis.get(key)
                    values = [json.loads(raw)] if raw is not None else []
                else:
                    # Bound each read; retained history may be larger than one turn.
                    cursor = "-"
                    while entries := await redis.xrange(key, min=cursor, count=256):
                        for _, value in entries:
                            unprotect_po_payload(key, value)
                        cursor = f"({entries[-1][0]}"
                    values = []
                for value in values:
                    unprotect_po_payload(key, value)
            except Exception:
                invalid += 1
        if invalid:
            raise POPayloadProtectionError(
                f"PO Redis startup refused: invalid_payload_keys={invalid}; run quiesced upgrade"
            )
    except POPayloadProtectionError:
        raise
    except Exception:
        raise POPayloadProtectionError("PO Redis startup readback failed") from None
