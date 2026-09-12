"""A consumer group must not be created at `$` where an in-flight message can be lost.

`$` delivers only what is published after the group exists. Where the group is
created lazily — inside the read loop, after a read failed with NOGROUP — a
message that landed in between is never delivered to anyone: the group owns the
stream position and no other consumer will read it.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
import redis.asyncio as redis

from shared.contracts.queues.worker import WorkerOwnership

_OWNERSHIP = WorkerOwnership(project_id="proj-1", run_id="run-1", attempt_id="eng-attempt-1")


class FakeStreams:
    """The slice of Redis Streams these paths depend on: group start positions."""

    def __init__(self) -> None:
        self.entries: dict[str, list[tuple[str, dict]]] = {}
        # stream -> group -> index of the next entry to deliver
        self.groups: dict[str, dict[str, int]] = {}
        self.created_at: list[tuple[str, str, str]] = []

    def publish(self, stream: str, payload: dict) -> str:
        entries = self.entries.setdefault(stream, [])
        msg_id = f"{len(entries) + 1}-0"
        entries.append((msg_id, {"data": json.dumps(payload)}))
        return msg_id

    async def xadd(self, stream, fields, **kwargs):
        entries = self.entries.setdefault(stream, [])
        msg_id = f"{len(entries) + 1}-0"
        entries.append((msg_id, dict(fields)))
        return msg_id

    async def xgroup_create(self, stream, group, id="$", mkstream=False):  # noqa: A002
        self.created_at.append((stream, group, id))
        existing = self.groups.setdefault(stream, {})
        if group in existing:
            raise redis.ResponseError("BUSYGROUP Consumer Group name already exists")
        self.entries.setdefault(stream, [])
        existing[group] = 0 if id == "0" else len(self.entries[stream])

    async def xreadgroup(self, groupname, consumername, streams, count=1, block=0):
        stream = next(iter(streams))
        if groupname not in self.groups.get(stream, {}):
            raise redis.ResponseError(f"NOGROUP No such consumer group '{groupname}'")
        cursor = self.groups[stream][groupname]
        entries = self.entries.get(stream, [])
        if cursor >= len(entries):
            return []
        self.groups[stream][groupname] = cursor + 1
        return [(stream, [entries[cursor]])]

    async def xgroup_destroy(self, stream, group):
        self.groups.get(stream, {}).pop(group, None)
        return 1

    async def xack(self, *args, **kwargs):
        return 1

    async def hget(self, *args, **kwargs):
        return "RUNNING"

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_nogroup_recovery_still_delivers_a_message_already_in_the_stream():
    """The lazy group creation in the read loop must not skip what is already queued."""
    from src.clients.worker_spawner import _wait_for_response

    fake = FakeStreams()
    stream = "worker:dev-123:output"
    fake.publish(stream, {"request_id": "req-1", "success": True, "output": "done"})

    resp = await _wait_for_response(
        fake,
        "langgraph-reuse-abc",
        "consumer-abc",
        None,
        5.0,
        stream,
    )

    assert resp is not None
    assert resp["output"] == "done"
    assert ("worker:dev-123:output", "langgraph-reuse-abc", "0") in fake.created_at


@pytest.mark.asyncio
async def test_reused_worker_output_group_starts_at_zero():
    """`send_task_to_worker` bootstraps the output group before the turn is sent."""
    from src.clients.worker_spawner import send_task_to_worker

    fake = FakeStreams()

    def _settings():
        class _S:
            redis_url = "redis://localhost:6379"

        return _S()

    with (
        patch("src.clients.worker_spawner.get_settings", _settings),
        patch("src.clients.worker_spawner.redis.from_url", return_value=fake),
        patch("src.clients.worker_spawner.record_worker_on_attempt", new_callable=AsyncMock),
        patch("src.clients.worker_spawner.record_turn_on_attempt", new_callable=AsyncMock),
        patch(
            "src.clients.worker_spawner._adopt_recorded_turn",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        await send_task_to_worker(
            worker_id="dev-123",
            task_content="do the thing",
            timeout_seconds=1,
            ownership=_OWNERSHIP,
        )

    output_groups = [c for c in fake.created_at if c[0] == "worker:dev-123:output"]
    assert output_groups, "no consumer group was created for the worker output stream"
    assert all(start == "0" for _, _, start in output_groups)
