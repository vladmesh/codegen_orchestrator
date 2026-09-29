"""A reclaimed first turn uses native checkout evidence, never old remote A."""

from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.queues.worker import WorkerOwnership


@pytest.mark.asyncio
@pytest.mark.parametrize("published", [False, True])
async def test_reconcile_baseline_survives_lost_response_and_saved_turn(published):
    from src.clients.worker_spawner import reconcile_prepared_baseline

    ownership = WorkerOwnership(
        project_id="project", run_id="init", attempt_id="eng", story_id="story"
    )
    metadata = {
        "initiating_run_id": "init",
        "worker_id": "worker",
        "pre_attempt_head_sha": "a" * 40,
    }
    if published:
        metadata["active_turn_request_id"] = "published-turn"
        metadata["pre_attempt_head_sha"] = "b" * 40
    row = {
        "id": "eng",
        "project_id": "project",
        "story_id": "story",
        "type": "engineering",
        "run_metadata": metadata,
    }
    redis = AsyncMock()
    native = {
        "project_id": "project",
        "run_id": "init",
        "attempt_id": "eng",
        "story_id": "story",
        "prepared_head_sha": "b" * 40,
    }
    redis.hgetall.side_effect = lambda key: {} if "active-turn" in key else native

    async def read(path):
        assert path == "runs/eng"
        return deepcopy(row)

    async def persist(path, *, json):
        row["run_metadata"].update(json["run_metadata"])
        raise OSError("lost baseline response")

    with (
        patch("src.clients.api.api_client.get", side_effect=read),
        patch("src.clients.api.api_client.patch", side_effect=persist) as write,
    ):
        if published:
            saved = await reconcile_prepared_baseline(redis, ownership, "worker")
            assert saved.pre_attempt_head_sha == "b" * 40
            assert saved.active_turn_request_id == "published-turn"
            write.assert_not_awaited()
        else:
            with pytest.raises(OSError, match="lost baseline response"):
                await reconcile_prepared_baseline(redis, ownership, "worker")
            saved = await reconcile_prepared_baseline(redis, ownership, "worker")
            assert saved.pre_attempt_head_sha == "b" * 40
            assert saved.prepared_checkout.attempt_id == "eng"
            assert write.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["project_id", "run_id", "story_id", "prepared_head_sha"])
async def test_missing_or_foreign_preparation_cannot_publish_first_turn(field):
    from src.clients.worker_spawner import reconcile_prepared_baseline

    ownership = WorkerOwnership(
        project_id="project", run_id="init", attempt_id="eng", story_id="story"
    )
    redis = AsyncMock()
    native = {
        "project_id": "project",
        "run_id": "init",
        "attempt_id": "eng",
        "story_id": "story",
        "prepared_head_sha": "b" * 40,
    }
    native.pop(field)
    redis.hgetall.return_value = native
    row = {
        "id": "eng",
        "project_id": "project",
        "story_id": "story",
        "type": "engineering",
        "run_metadata": {"initiating_run_id": "init"},
    }
    with (
        patch("src.clients.api.api_client.get", AsyncMock(return_value=row)),
        patch("src.clients.api.api_client.patch", new_callable=AsyncMock) as write,
        pytest.raises(RuntimeError),
    ):
        await reconcile_prepared_baseline(redis, ownership, "worker")
    write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "inconsistency", ["published_baseline", "active_lease", "saved_worker", "saved_preparation"]
)
async def test_inconsistent_saved_turn_fails_before_any_write(inconsistency):
    from datetime import UTC, datetime, timedelta

    from shared.contracts.worker_turn import WorkerActiveTurn
    from src.clients.worker_spawner import reconcile_prepared_baseline

    ownership = WorkerOwnership(
        project_id="project", run_id="init", attempt_id="eng", story_id="story"
    )
    native = {
        "project_id": "project",
        "run_id": "init",
        "attempt_id": "eng",
        "story_id": "story",
        "prepared_head_sha": "b" * 40,
    }
    metadata = {
        "initiating_run_id": "init",
        "worker_id": "worker",
        "pre_attempt_head_sha": "b" * 40,
        "active_turn_request_id": "turn",
    }
    active = {}
    if inconsistency == "published_baseline":
        metadata["pre_attempt_head_sha"] = "a" * 40
    elif inconsistency == "saved_worker":
        metadata["worker_id"] = "foreign-worker"
    elif inconsistency == "saved_preparation":
        metadata["prepared_checkout"] = {
            "worker_id": "worker",
            "attempt_id": "eng",
            "head_sha": "a" * 40,
        }
    else:
        now = datetime.now(UTC)
        active = WorkerActiveTurn(
            worker_id="worker",
            attempt_id="foreign-attempt",
            request_id="foreign-turn",
            lease_id="lease",
            started_at=now,
            deadline_at=now + timedelta(minutes=1),
        ).as_redis_fields()
    redis = AsyncMock()
    redis.hgetall.side_effect = lambda key: active if "active-turn" in key else native
    row = {
        "id": "eng",
        "project_id": "project",
        "story_id": "story",
        "type": "engineering",
        "run_metadata": metadata,
    }
    with (
        patch("src.clients.api.api_client.get", AsyncMock(return_value=row)),
        patch("src.clients.api.api_client.patch", new_callable=AsyncMock) as write,
        pytest.raises(RuntimeError),
    ):
        await reconcile_prepared_baseline(redis, ownership, "worker")
    write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("genuine_commit", [False, True])
@pytest.mark.parametrize("published", [False, True])
async def test_actual_reclaimed_worker_turn_rejects_checkout_only_result(genuine_commit, published):
    from shared.contracts.dto.engineering import EngineeringStatus
    from shared.contracts.dto.run_result import EngineeringFailureReason
    from shared.contracts.worker_turn import AttemptTurnMetadata
    from src.clients import worker_spawner
    from src.nodes.developer import DeveloperNode
    from tests.unit.test_developer_node import TestNoNewCommitOnStoryBranch, _CommitGraph

    state = TestNoNewCommitOnStoryBranch._story_state()
    state["worker_id"] = "worker"
    old = {"initiating_run_id": "live-1", "worker_id": "worker", "pre_attempt_head_sha": "a" * 40}
    # A published turn started at B; the consumer still holds stale A.
    row = {
        "id": "eng-1",
        "project_id": "proj-1",
        "story_id": "story-1",
        "type": "engineering",
        "run_metadata": dict(old),
    }
    if published:
        row["run_metadata"].update(pre_attempt_head_sha="b" * 40, active_turn_request_id="old-turn")
    state["attempt_turn"] = AttemptTurnMetadata.from_run_metadata(old)
    redis = AsyncMock()
    redis.hgetall.side_effect = lambda key: (
        {}
        if "active-turn" in key
        else {
            "project_id": "proj-1",
            "run_id": "live-1",
            "attempt_id": "eng-1",
            "story_id": "story-1",
            "prepared_head_sha": "b" * 40,
        }
    )
    reported = "c" * 40 if genuine_commit else "b" * 40
    output = {"status": "completed", "content": "Done", "commit_sha": reported}

    async def persist(path, *, json):
        row["run_metadata"].update(json["run_metadata"])

    adopted = TestNoNewCommitOnStoryBranch._reports(reported) if published else None
    with (
        patch("src.nodes.developer.GitHubAppClient") as gh,
        patch("src.nodes.developer.api_client") as api,
        patch("src.clients.api.api_client.get", AsyncMock(side_effect=lambda path: deepcopy(row))),
        patch("src.clients.api.api_client.patch", side_effect=persist),
        patch.object(worker_spawner.redis, "from_url", return_value=redis),
        patch.object(worker_spawner, "_wait_for_response", AsyncMock(return_value=output)),
        patch.object(worker_spawner, "await_turn_output", AsyncMock(return_value=adopted)) as adopt,
    ):
        graph = _CommitGraph(story_head="deployed-head")
        graph.commits.update(
            {"a" * 40: (None, True), "b" * 40: ("a" * 40, True), "c" * 40: ("b" * 40, True)}
        )
        graph.refs["heads/story/story-1"] = reported
        graph.install(gh)
        TestNoNewCommitOnStoryBranch._api(api)
        outcome = await DeveloperNode().run(state)
    if genuine_commit:
        assert outcome["engineering_status"] is EngineeringStatus.DONE
    else:
        assert outcome["engineering_status"] is EngineeringStatus.FAILED
        assert outcome["failure_reason"] is EngineeringFailureReason.NO_NEW_COMMIT
        assert "pull_request" not in outcome and "deploy" not in outcome
    assert row["run_metadata"]["pre_attempt_head_sha"] == "b" * 40
    if published:
        redis.xadd.assert_not_awaited()
        assert adopt.await_args.kwargs["request_id"] == "old-turn"
    else:
        redis.xadd.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_creator", [False, True])
async def test_later_attempt_preserves_own_baseline_and_requires_real_creator(foreign_creator):
    from src.clients.worker_spawner import reconcile_prepared_baseline

    ownership = WorkerOwnership(
        project_id="project", run_id="init", attempt_id="later", story_id="story"
    )
    redis = AsyncMock()
    native = {
        "project_id": "project",
        "run_id": "init",
        "attempt_id": "creator",
        "story_id": "story",
        "prepared_head_sha": "b" * 40,
    }
    redis.hgetall.side_effect = lambda key: {} if "active-turn" in key else native
    later = {
        "id": "later",
        "project_id": "project",
        "story_id": "story",
        "type": "engineering",
        "run_metadata": {
            "initiating_run_id": "init",
            "worker_id": "worker",
            "pre_attempt_head_sha": "c" * 40,
        },
    }
    creator = {**later, "id": "creator", "status": "completed"}
    if foreign_creator:
        creator["story_id"] = "foreign"
    with (
        patch("src.clients.api.api_client.get", AsyncMock(side_effect=[later, creator])),
        patch("src.clients.api.api_client.patch", new_callable=AsyncMock) as write,
    ):
        if foreign_creator:
            with pytest.raises(RuntimeError, match="creator"):
                await reconcile_prepared_baseline(redis, ownership, "worker")
        else:
            assert (
                await reconcile_prepared_baseline(redis, ownership, "worker")
            ).pre_attempt_head_sha == "c" * 40
    write.assert_not_awaited()
