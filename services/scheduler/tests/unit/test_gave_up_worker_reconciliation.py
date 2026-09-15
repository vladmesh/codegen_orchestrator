from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from shared.contracts.dto.engineering import EngineeringStatus
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.queues.worker import DeleteWorkerCommand
from shared.queues import STORY_WORKERS_KEY, WORKER_COMMANDS
from src.tasks.gave_up_worker_reconciliation import reconcile_gave_up_attempt_workers

STORY = "story-1"
PROJECT = "project-1"
T0 = datetime(2026, 9, 15, 12, tzinfo=UTC)


class FakeRedis:
    """Hashes, strings and the compare-and-delete script over plain dicts."""

    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self.strings: dict[str, str] = {}
        self.replacement_on_eval: str | None = None

    async def scan_iter(self, match):
        prefix = match.removesuffix("*")
        for key in list(self.hashes):
            if key.startswith(prefix):
                yield key

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def get(self, key):
        return self.strings.get(key)

    async def eval(self, script, count, key, field, expected):
        bindings = self.hashes.setdefault(key, {})
        if self.replacement_on_eval:
            bindings[field] = self.replacement_on_eval
        if bindings.get(field) != expected:
            return 0
        del bindings[field]
        return 1

    def spawn(self, worker_id, *, story_id=STORY, attempt_id="run-gave-up"):
        meta = {"project_id": PROJECT, "run_id": "initiating", "attempt_id": attempt_id}
        if story_id:
            meta["story_id"] = story_id
        self.hashes[f"worker:meta:{worker_id}"] = meta
        self.hashes[f"worker:status:{worker_id}"] = {"status": "RUNNING"}
        self.strings[f"workspace:lock:{PROJECT}"] = worker_id

    def bind(self, worker_id, story_id=STORY):
        self.hashes.setdefault(STORY_WORKERS_KEY, {})[story_id] = worker_id

    def confirm_removal(self, worker_id):
        """What worker-manager does once Docker confirms the teardown."""
        self.hashes.pop(f"worker:meta:{worker_id}", None)
        self.hashes.pop(f"worker:status:{worker_id}", None)
        if self.strings.get(f"workspace:lock:{PROJECT}") == worker_id:
            del self.strings[f"workspace:lock:{PROJECT}"]

    def binding(self, story_id=STORY):
        return self.hashes.get(STORY_WORKERS_KEY, {}).get(story_id)


class Client:
    def __init__(self, redis):
        self.redis = redis
        self.published: list[DeleteWorkerCommand] = []
        self.lose_next_publish = False

    async def publish(self, queue, payload):
        assert queue == WORKER_COMMANDS
        if self.lose_next_publish:
            self.lose_next_publish = False
            raise RuntimeError("redis unavailable")
        self.published.append(DeleteWorkerCommand.model_validate(payload))

    def deleted(self):
        return [command.worker_id for command in self.published]


def _run(run_id, status, *, minutes=0, engineering_status=None, run_type=RunType.ENGINEERING):
    result = None
    if engineering_status is not None:
        result = SimpleNamespace(engineering_status=engineering_status)
    return SimpleNamespace(
        id=run_id,
        type=run_type,
        status=status,
        project_id=PROJECT,
        story_id=STORY,
        result=result,
        created_at=T0 + timedelta(minutes=minutes),
    )


def _gave_up_run(run_id="run-gave-up", minutes=0):
    return _run(
        run_id, RunStatus.FAILED, minutes=minutes, engineering_status=EngineeringStatus.GAVE_UP
    )


class FakeApi:
    def __init__(self, runs):
        self.runs = runs

    async def list_story_runs(self, story_id):
        return sorted(
            (run for run in self.runs if run.story_id == story_id),
            key=lambda run: run.created_at,
            reverse=True,
        )

    async def get_run_if_missing_returns_none(self, run_id):
        return next((run for run in self.runs if run.id == run_id), None)


@pytest.mark.asyncio
async def test_gave_up_attempt_worker_is_removed_and_binding_cleared_after_confirmation():
    # The story never left IN_PROGRESS: the handler settled the run and crashed.
    # Only the settled attempt says it gave up, and that is enough.
    redis = FakeRedis()
    redis.spawn("worker-gave-up")
    redis.bind("worker-gave-up")
    client = Client(redis)
    api = FakeApi([_gave_up_run()])

    assert await reconcile_gave_up_attempt_workers(api, client) == 0
    assert client.deleted() == ["worker-gave-up"]
    assert client.published[0].reason == "failed"
    # Not observed yet: the binding and the metadata stay as the retry record.
    assert redis.binding() == "worker-gave-up"
    assert "worker:meta:worker-gave-up" in redis.hashes

    redis.confirm_removal("worker-gave-up")
    assert await reconcile_gave_up_attempt_workers(api, client) == 1
    assert redis.binding() is None

    assert await reconcile_gave_up_attempt_workers(api, client) == 0
    assert client.deleted() == ["worker-gave-up", "worker-gave-up"]


@pytest.mark.asyncio
async def test_lost_removal_request_is_re_driven_on_a_later_tick():
    redis = FakeRedis()
    redis.spawn("worker-gave-up")
    redis.bind("worker-gave-up")
    client = Client(redis)
    api = FakeApi([_gave_up_run()])

    client.lose_next_publish = True
    assert await reconcile_gave_up_attempt_workers(api, client) == 0
    assert client.deleted() == []
    assert redis.binding() == "worker-gave-up"

    # Published but worker-manager has not confirmed: still retried.
    assert await reconcile_gave_up_attempt_workers(api, client) == 0
    assert await reconcile_gave_up_attempt_workers(api, client) == 0
    assert client.deleted() == ["worker-gave-up", "worker-gave-up"]
    assert redis.binding() == "worker-gave-up"

    redis.confirm_removal("worker-gave-up")
    assert await reconcile_gave_up_attempt_workers(api, client) == 1
    assert redis.binding() is None


@pytest.mark.asyncio
async def test_binding_left_after_metadata_is_gone_is_still_cleared():
    redis = FakeRedis()
    redis.bind("worker-gave-up")
    client = Client(redis)

    assert await reconcile_gave_up_attempt_workers(FakeApi([_gave_up_run()]), client) == 1
    assert client.deleted() == ["worker-gave-up"]
    assert redis.binding() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("live_status", [RunStatus.QUEUED, RunStatus.RUNNING])
async def test_newer_live_attempt_worker_is_never_removed(live_status):
    redis = FakeRedis()
    redis.spawn("worker-gave-up")
    redis.spawn("worker-live", attempt_id="run-live")
    redis.bind("worker-live")
    client = Client(redis)
    api = FakeApi([_gave_up_run(), _run("run-live", live_status, minutes=5)])

    assert await reconcile_gave_up_attempt_workers(api, client) == 0
    assert client.deleted() == []
    assert redis.binding() == "worker-live"


@pytest.mark.asyncio
async def test_replacement_bound_during_teardown_keeps_its_binding_and_worker():
    redis = FakeRedis()
    redis.bind("worker-gave-up")
    redis.replacement_on_eval = "worker-replacement"
    client = Client(redis)

    assert await reconcile_gave_up_attempt_workers(FakeApi([_gave_up_run()]), client) == 0
    assert client.deleted() == ["worker-gave-up"]
    assert redis.binding() == "worker-replacement"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "newest",
    [
        _run("done", RunStatus.COMPLETED, minutes=5, engineering_status=EngineeringStatus.DONE),
        _run("failed", RunStatus.FAILED, minutes=5, engineering_status=EngineeringStatus.FAILED),
    ],
    ids=["completed-keeps-story-worker", "technical-failure-left-to-retry"],
)
async def test_worker_is_untouched_unless_the_newest_attempt_gave_up(newest):
    redis = FakeRedis()
    redis.spawn("worker-story")
    redis.bind("worker-story")
    client = Client(redis)

    assert await reconcile_gave_up_attempt_workers(FakeApi([_gave_up_run(), newest]), client) == 0
    assert client.deleted() == []
    assert redis.binding() == "worker-story"


@pytest.mark.asyncio
async def test_storyless_gave_up_attempt_worker_is_removed():
    redis = FakeRedis()
    redis.spawn("worker-storyless", story_id=None, attempt_id="run-storyless")
    redis.spawn("worker-other", story_id=None, attempt_id="run-running")
    client = Client(redis)
    storyless = _gave_up_run("run-storyless")
    storyless.story_id = None
    running = _run("run-running", RunStatus.RUNNING)
    running.story_id = None
    api = FakeApi([storyless, running])

    assert await reconcile_gave_up_attempt_workers(api, client) == 0
    assert client.deleted() == ["worker-storyless"]

    redis.confirm_removal("worker-storyless")
    assert await reconcile_gave_up_attempt_workers(api, client) == 0
    assert client.deleted() == ["worker-storyless"]
