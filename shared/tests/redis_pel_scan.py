"""An XAUTOCLAIM stand-in that expresses the scan limit fakeredis cannot.

Shared by every suite that has to prove a PEL sweep walks to the end of the
list, so there is one model of the command's awkward answer rather than one per
consumer:

- `shared/tests/test_redis_client.py` — the sweep inside `RedisStreamClient`;
- `services/langgraph/tests/unit/po/test_consumer_pel.py` — the PO consumer
  reading through that client.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PelEntry:
    """One entry in a consumer group's PEL, as the scan below sees it."""

    entry_id: str
    idle_ms: int
    fields: dict[str, str] = field(default_factory=lambda: {"data": "{}"})


class RedisPelScan:
    """XAUTOCLAIM with the scan limit real Redis applies and fakeredis does not.

    Redis walks at most about ``COUNT * 10`` PEL entries per call and then
    answers with wherever it stopped, so a call can come back with an advanced
    cursor, no claimed entries and no deleted ids. fakeredis instead filters the
    whole PEL by idle time and returns ``start`` as the cursor whenever it
    claimed nothing, which cannot express that answer. Reproduced against
    ``redis:7.4.10-alpine``: eleven pending entries, the first ten refreshed,
    the eleventh stale, ``COUNT 1`` -> ``cursor=<eleventh-id>, claimed=[],
    deleted=[]``.
    """

    def __init__(self, entries: list[PelEntry]):
        self.entries = entries
        self.start_ids: list[str] = []

    async def __call__(self, stream, group, consumer, *, min_idle_time, start_id, count):
        self.start_ids.append(start_id)
        cursor = "0-0"
        claimed: list[tuple[str, dict[str, str]]] = []
        scanned = 0
        for entry in (e for e in self.entries if e.entry_id >= start_id):
            if scanned >= count * 10 or len(claimed) >= count:
                cursor = entry.entry_id
                break
            scanned += 1
            if entry.idle_ms >= min_idle_time:
                claimed.append((entry.entry_id, entry.fields))
        return [cursor, claimed, []]
