from types import SimpleNamespace

import pytest

from shared.queues import STORY_WORKERS_KEY, WORKER_COMMANDS
from src.tasks.story_worker_teardown import finalize_story_worker_teardown


class RedisState:
    def __init__(self, *, binding="worker-old", status=None, meta=None, lock=None):
        self.binding = binding
        self.status = status
        self.meta = meta
        self.lock = lock
        self.replacement_on_eval = None

    async def hget(self, key, field):
        if key == STORY_WORKERS_KEY:
            return self.binding
        if key == "worker:status:worker-old":
            return self.status
        return None

    async def hgetall(self, key):
        if key == "worker:status:worker-old" and self.status:
            return {"status": self.status}
        return self.meta if key == "worker:meta:worker-old" and self.meta else {}

    async def get(self, key):
        return self.lock

    async def eval(self, script, count, key, field, expected):
        if self.replacement_on_eval:
            self.binding = self.replacement_on_eval
        if self.binding != expected:
            return 0
        self.binding = None
        return 1


class Client(SimpleNamespace):
    def __init__(self, redis, publish=None):
        self.redis = redis
        self.published = []
        self._publish = publish

    async def publish(self, queue, payload):
        if self._publish:
            return await self._publish(queue, payload)
        self.published.append((queue, payload))


@pytest.mark.asyncio
async def test_publish_failure_retains_story_binding():
    state = RedisState()

    async def fail(*_):
        raise RuntimeError("redis unavailable")

    assert not await finalize_story_worker_teardown(
        Client(state, fail), story_id="story-1", project_id="project-1", request_id="request-1"
    )
    assert state.binding == "worker-old"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "meta", "lock"),
    [("RUNNING", None, None), (None, {"story_id": "story-1"}, None), (None, None, "worker-old")],
)
async def test_incomplete_removal_observation_retains_story_binding(
    monkeypatch, status, meta, lock
):
    monkeypatch.setattr("src.tasks.story_worker_teardown.TEARDOWN_OBSERVATIONS", 1)
    state = RedisState(status=status, meta=meta, lock=lock)

    assert not await finalize_story_worker_teardown(
        Client(state), story_id="story-1", project_id="project-1", request_id="request-1"
    )
    assert state.binding == "worker-old"


@pytest.mark.asyncio
async def test_absent_worker_evidence_compare_clears_crash_recovery_binding():
    state = RedisState()
    client = Client(state)

    assert await finalize_story_worker_teardown(
        client, story_id="story-1", project_id="project-1", request_id="request-1"
    )
    assert state.binding is None
    assert client.published[0][0] == WORKER_COMMANDS
    assert client.published[0][1]["worker_id"] == "worker-old"


@pytest.mark.asyncio
async def test_replacement_binding_race_is_never_cleared_and_blocks_handoff():
    state = RedisState()
    state.replacement_on_eval = "worker-new"

    assert not await finalize_story_worker_teardown(
        Client(state), story_id="story-1", project_id="project-1", request_id="request-1"
    )
    assert state.binding == "worker-new"


@pytest.mark.asyncio
@pytest.mark.parametrize("downstream_fix", ["qa_fix", "deploy_fix"])
async def test_pr_teardown_leaves_downstream_fix_no_dead_worker_to_reuse(downstream_fix):
    state = RedisState()

    assert await finalize_story_worker_teardown(
        Client(state), story_id="story-1", project_id="project-1", request_id="request-1"
    )

    # Both repair paths consult this exact registry before choosing reuse or spawn.
    assert await state.hget(STORY_WORKERS_KEY, "story-1") is None, downstream_fix
