"""Service coverage for an engineering result that carried no new commit.

The stand case this reproduces: a taskless deploy-fix worker finished reporting
the SHA already deployed for the story. The consumer must end that attempt as a
failed Run with its own reason, publish no deploy, and take the story out of
`in_progress` — where `complete_stories` would otherwise retry a pull request
GitHub refuses with 422 "No commits between" for ever.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

import httpx
from psycopg import AsyncConnection
from psycopg.rows import dict_row
import pytest
from structlog.testing import capture_logs

from shared.contracts.dto.executor_decision import ExecutorDecision, ExecutorDecisionSource
from shared.contracts.dto.owner_notification import OwnerNotification, OwnerNotificationState
from shared.contracts.dto.run import RunType
from shared.contracts.dto.run_result import EngineeringFailureReason
from shared.contracts.dto.story_failure import StoryFailureCode
from shared.contracts.vocab import AgentType
from shared.contracts.worker_turn import AttemptTurnMetadata
from shared.queues import DEPLOY_QUEUE
from shared.redis import RedisStreamClient
from tests.unit.factories import make_project, make_repository

_DEPLOYED_HEAD = "d159f7d"
_STORY_ID = "story-1"
_ATTEMPT_ID = "eng-deploy-fix-deploy-poll-0a09250a-1"


def _engineering_message() -> dict:
    """A taskless deploy-fix job: story-owned, no planning task, deploy expected."""
    return {
        "task_id": _ATTEMPT_ID,
        "project_id": str(make_project().id),
        "initiating_run_id": "live-run-1",
        "telegram_chat_id": "",
        "action": "fix",
        "description": "Repair the failing deploy",
        "skip_deploy": False,
        "planning_task_id": None,
        "story_id": _STORY_ID,
        "deploy_fix_attempt": 1,
    }


async def _github_reporting_a_deployed_head():
    """A story branch whose head, before the repair started, is the deployed commit."""
    client = AsyncMock()
    client.get_repo_scoped_token = AsyncMock(return_value="ghs_fake")
    client.get_repo = AsyncMock(return_value=SimpleNamespace(default_branch="main"))
    client.get_ref_sha = AsyncMock(return_value=_DEPLOYED_HEAD)
    client.branch_contains_commit = AsyncMock(return_value=True)
    client.commit_adds_changes = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_sha", [None, "", _DEPLOYED_HEAD, "older-head", "net-empty-head"])
async def test_reported_deployed_head_fails_the_run_and_parks_the_story(real_redis, commit_sha):
    """The whole consumer path, from worker result to Run, story and deploy queue."""
    from src.clients.worker_spawner import SpawnResult
    from src.consumers.engineering import process_engineering_job

    await real_redis.delete(DEPLOY_QUEUE)

    api = AsyncMock()
    api.get_project = AsyncMock(return_value=make_project())
    api.get_primary_repository = AsyncMock(return_value=make_repository())
    api.get_run = AsyncMock(return_value=SimpleNamespace(run_metadata={}))
    api.transition_story = AsyncMock()

    github = await _github_reporting_a_deployed_head()
    redis_client = RedisStreamClient()
    await redis_client.connect()

    try:
        with (
            patch("src.consumers.engineering.api_client", api),
            patch("src.consumers.engineering_result_handler.api_client", api),
            patch("src.nodes.developer.api_client", api),
            patch("src.nodes.developer.GitHubAppClient", return_value=github),
            patch(
                "src.consumers.engineering._load_engineering_executor_decision",
                new=AsyncMock(
                    return_value=ExecutorDecision(
                        attempt_kind=RunType.ENGINEERING,
                        agent_type=AgentType.CLAUDE,
                        source=ExecutorDecisionSource.API_DEFAULT,
                        policy_version="v1",
                        reason="Engineering executor selected by API DEFAULT_AGENT_TYPE.",
                    )
                ),
            ),
            patch(
                "src.consumers.engineering._resolve_allocations",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "src.consumers.engineering._recorded_attempt_turn",
                new=AsyncMock(return_value=AttemptTurnMetadata()),
            ),
            patch(
                "src.consumers.engineering._build_story_context", new=AsyncMock(return_value=None)
            ),
            patch("src.consumers.engineering._build_story_md", new=AsyncMock(return_value=None)),
            patch(
                "src.nodes.developer.request_spawn",
                new=AsyncMock(
                    return_value=SpawnResult(
                        request_id="req-1",
                        success=True,
                        exit_code=0,
                        output="The deployment already looks correct",
                        commit_sha=commit_sha,
                        turn_result_consumed=True,
                    )
                ),
            ),
        ):
            outcome = await process_engineering_job(_engineering_message(), redis_client)

        assert outcome["status"] == "failed"

        run_patches = [
            call for call in api.patch.await_args_list if call.args[0] == f"runs/{_ATTEMPT_ID}"
        ]
        terminal = run_patches[-1].kwargs["json"]
        assert terminal["status"] == "failed"
        assert terminal["result"]["failure_reason"] == EngineeringFailureReason.NO_NEW_COMMIT.value
        assert "commit" in terminal["error_message"]

        assert not any(call.args[0] == f"stories/{_STORY_ID}" for call in api.patch.await_args_list)
        api.stop_story.assert_awaited_once()
        assert api.stop_story.await_args.args[:2] == (_STORY_ID, "human-review")
        assert api.stop_story.await_args.args[2].code is StoryFailureCode.NO_NEW_COMMIT
        api.transition_story.assert_not_awaited()

        # Nothing was handed to deploy: the SHA is already deployed.
        assert await real_redis.xlen(DEPLOY_QUEUE) == 0
    finally:
        await redis_client.close()
        await real_redis.delete(DEPLOY_QUEUE)


@pytest.fixture
async def durable_api(real_redis):
    """A real control API/DB for this consumer, alongside its legacy API fixture."""
    from datetime import UTC, datetime, timedelta

    from shared.contracts.dto.executor_diagnostics import (
        EXECUTOR_DIAGNOSTICS_REDIS_KEY,
        ExecutorDiagnostic,
        ExecutorDiagnosticSnapshot,
    )
    from shared.tests.executor_diagnostic_cases import host_profile_for_reason
    from src.clients.api import LanggraphAPIClient

    now = datetime.now(UTC)
    expiry = now + timedelta(minutes=5)
    await real_redis.set(
        EXECUTOR_DIAGNOSTICS_REDIS_KEY,
        ExecutorDiagnosticSnapshot(
            schema_version="v2",
            version="empty-result-test",
            observed_at=now,
            expires_at=expiry,
            diagnostics=[
                ExecutorDiagnostic(
                    executor=executor,
                    enabled=True,
                    auth_mode="host_session",
                    availability="available",
                    observed_at=now,
                    expires_at=expiry,
                    active_lease_count=0,
                    reason_code="ready",
                    reason="Local authentication and worker inventory are ready.",
                    profile=host_profile_for_reason("ready"),
                )
                for executor in (AgentType.CLAUDE, AgentType.CODEX)
            ],
        ).model_dump_json(),
        ex=300,
    )
    api = LanggraphAPIClient()
    api.base_url = os.environ["TEST_API_BASE_URL"]
    telegram_id = uuid.uuid4().int % 1_000_000_000
    await api.post("users/", json={"telegram_id": telegram_id, "username": f"empty_{telegram_id}"})
    project = await api.post(
        "projects/",
        json={
            "title": "Empty engineering",
            "initiating_run_id": "fixture-init",
            "status": "active",
            "config": {},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    project_id = project["id"]
    await api.post(
        "repositories/",
        json={
            "project_id": project_id,
            "name": f"empty-{uuid.uuid4().hex[:8]}",
            "git_url": "https://github.com/fixture/empty.git",
        },
    )
    story = await api.post("stories/", json={"project_id": project_id, "title": "Empty result"})
    story_id = story["id"]
    await api.transition_story(story_id, "start")
    run_id = f"eng-empty-{uuid.uuid4().hex[:12]}"
    admitted = await api.post(
        "work-admission/paid-runs",
        json={
            "id": run_id,
            "type": "engineering",
            "project_id": project_id,
            "story_id": story_id,
            "run_metadata": {"deploy_fix_attempt": 1},
        },
    )
    assert admitted["run_id"] == run_id
    yield api, project_id, story_id, run_id
    await api.close()


async def _durable_stop_row(story_id):
    async with await AsyncConnection.connect(
        os.environ["TEST_DATABASE_URL"], row_factory=dict_row
    ) as connection:
        cursor = await connection.execute(
            "SELECT status, waiting_on, quarantine_reason, owner_notification "
            "FROM stories WHERE id=%s",
            (story_id,),
        )
        return await cursor.fetchone()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "commit_sha", [None, "", _DEPLOYED_HEAD, "older-head", "net-empty-head", "a" * 40]
)
@pytest.mark.parametrize("interrupted", [False, True])
async def test_actual_empty_processing_commits_the_story_reason_and_owed_notices(
    real_redis,
    durable_api,
    commit_sha,
    interrupted,
):
    from src.clients.worker_spawner import SpawnResult
    from src.consumers.engineering import process_engineering_job

    api, project_id, story_id, run_id = durable_api
    github = await _github_reporting_a_deployed_head()
    stream = RedisStreamClient()
    await stream.connect()
    before_deploys = await real_redis.xlen(DEPLOY_QUEUE)
    github.commit_adds_changes.return_value = commit_sha == "a" * 40
    original_publish = stream.publish_flat

    async def interrupted_publish(queue, fields):
        if interrupted and fields.get("event") == "failed":
            raise ConnectionError("CALLBACK_CANARY")
        await original_publish(queue, fields)

    try:
        with (
            patch("src.consumers.engineering.api_client", api),
            patch("src.consumers.engineering_result_handler.api_client", api),
            patch("src.nodes.developer.api_client", api),
            patch("src.nodes.developer.GitHubAppClient", return_value=github),
            patch("src.consumers.engineering._resolve_allocations", AsyncMock(return_value={})),
            patch("src.consumers.engineering._build_story_context", AsyncMock(return_value=None)),
            patch("src.consumers.engineering._build_story_md", AsyncMock(return_value=None)),
            patch(
                "src.nodes.developer.request_spawn",
                AsyncMock(
                    return_value=SpawnResult(
                        request_id="fixture-turn",
                        success=True,
                        exit_code=0,
                        commit_sha=commit_sha,
                        output="Authorization: Bearer EMPTY_OUTPUT_CANARY",
                        turn_result_consumed=True,
                    )
                ),
            ),
            patch(
                "src.consumers.engineering_result_handler._uncomputable_derived_keys_at",
                AsyncMock(return_value=[]),
            ),
            patch.object(stream, "publish_flat", side_effect=interrupted_publish),
            patch.object(api, "patch", wraps=api.patch) as writes,
            capture_logs() as logs,
        ):
            outcome = await process_engineering_job(
                {
                    **_engineering_message(),
                    "task_id": run_id,
                    "project_id": project_id,
                    "story_id": story_id,
                    "callback_stream": f"fixture:callback:{run_id}" if interrupted else None,
                },
                stream,
            )
        if commit_sha == "a" * 40:
            assert outcome["status"] == "success"
            assert (await api.get_run(run_id)).status.value == "completed"
            row = await _durable_stop_row(story_id)
            assert row["status"] == "in_progress"
            assert row["quarantine_reason"] is None
            assert row["owner_notification"] is None
            assert await real_redis.xlen(DEPLOY_QUEUE) == before_deploys + 1
            return
        assert outcome["status"] == "failed"
        run = await api.get_run(run_id)
        assert run.status.value == "failed"
        assert run.result.failure_reason is EngineeringFailureReason.NO_NEW_COMMIT
        assert len((await api.get(f"runs/{run_id}"))["error_message"]) <= 503
        row = await _durable_stop_row(story_id)
        assert row["status"] == "waiting_human_review"
        assert row["waiting_on"] == "human_review"
        cause = row["quarantine_reason"]
        assert cause["code"] == "no_new_commit"
        notification = OwnerNotification.model_validate(row["owner_notification"])
        assert notification.state is OwnerNotificationState.OWED
        assert notification.admin_state is OwnerNotificationState.OWED
        assert notification.event.value == "story_blocked"
        assert "nothing was produced" in notification.text
        assert "a person" in notification.text
        assert run_id in cause["detail"]
        assert "EMPTY_OUTPUT_CANARY" not in json.dumps([row, logs])
        assert "CALLBACK_CANARY" not in json.dumps(logs)
        assert not any(call.args[0] == f"stories/{story_id}" for call in writes.await_args_list)
        assert await real_redis.xlen(DEPLOY_QUEUE) == before_deploys
    finally:
        await stream.close()


@pytest.mark.asyncio
async def test_actual_stop_refusal_leaves_the_attempt_reclaimable(real_redis, durable_api):
    from src.clients.worker_spawner import SpawnResult
    from src.consumers.engineering import process_engineering_job
    from src.consumers.engineering_result_handler import StoryStopError

    api, project_id, story_id, run_id = durable_api
    stream = RedisStreamClient()
    await stream.connect()
    try:
        with (
            patch("src.consumers.engineering.api_client", api),
            patch("src.consumers.engineering_result_handler.api_client", api),
            patch("src.nodes.developer.api_client", api),
            patch(
                "src.nodes.developer.GitHubAppClient",
                return_value=await _github_reporting_a_deployed_head(),
            ),
            patch("src.consumers.engineering._resolve_allocations", AsyncMock(return_value={})),
            patch("src.consumers.engineering._build_story_context", AsyncMock(return_value=None)),
            patch("src.consumers.engineering._build_story_md", AsyncMock(return_value=None)),
            patch(
                "src.nodes.developer.request_spawn",
                AsyncMock(
                    return_value=SpawnResult(
                        request_id="fixture-turn",
                        success=True,
                        exit_code=0,
                        output="",
                        turn_result_consumed=True,
                    )
                ),
            ),
            patch.object(
                api, "stop_story", side_effect=RuntimeError("Authorization: Bearer REFUSAL_CANARY")
            ),
            capture_logs() as logs,
        ):
            with pytest.raises(StoryStopError):
                await process_engineering_job(
                    {
                        **_engineering_message(),
                        "task_id": run_id,
                        "project_id": project_id,
                        "story_id": story_id,
                    },
                    stream,
                )
        assert (await api.get_run(run_id)).status.value == "running"
        row = await _durable_stop_row(story_id)
        assert row["status"] == "in_progress"
        assert row["quarantine_reason"] is None
        assert row["owner_notification"] is None
        assert "REFUSAL_CANARY" not in json.dumps(logs)
    finally:
        await stream.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_sha", [None, _DEPLOYED_HEAD])
async def test_actual_planned_empty_attempt_fails_its_task_without_parking_the_story(
    real_redis,
    durable_api,
    commit_sha,
):
    from src.clients.worker_spawner import SpawnResult
    from src.consumers.engineering import process_engineering_job

    api, project_id, story_id, run_id = durable_api
    task = await api.post(
        "tasks/",
        json={
            "project_id": project_id,
            "story_id": story_id,
            "title": "Planned change",
            "status": "in_dev",
            "max_iterations": 3,
        },
    )
    stream = RedisStreamClient()
    await stream.connect()
    before_deploys = await real_redis.xlen(DEPLOY_QUEUE)
    try:
        with (
            patch("src.consumers.engineering.api_client", api),
            patch("src.consumers.engineering_result_handler.api_client", api),
            patch("src.nodes.developer.api_client", api),
            patch(
                "src.nodes.developer.GitHubAppClient",
                return_value=await _github_reporting_a_deployed_head(),
            ),
            patch("src.consumers.engineering._resolve_allocations", AsyncMock(return_value={})),
            patch("src.consumers.engineering._build_story_context", AsyncMock(return_value=None)),
            patch("src.consumers.engineering._build_story_md", AsyncMock(return_value=None)),
            patch(
                "src.nodes.developer.request_spawn",
                AsyncMock(
                    return_value=SpawnResult(
                        request_id="planned-turn",
                        success=True,
                        exit_code=0,
                        output="",
                        commit_sha=commit_sha,
                        turn_result_consumed=True,
                    )
                ),
            ),
        ):
            outcome = await process_engineering_job(
                {
                    **_engineering_message(),
                    "task_id": run_id,
                    "project_id": project_id,
                    "story_id": story_id,
                    "planning_task_id": task["id"],
                },
                stream,
            )
        assert outcome["status"] == "failed"
        assert (
            await api.get_run(run_id)
        ).result.failure_reason is EngineeringFailureReason.NO_NEW_COMMIT
        failed = await api.get_task(task["id"])
        assert failed.status.value == "failed"
        assert (failed.current_iteration, failed.max_iterations) == (0, 3)
        row = await _durable_stop_row(story_id)
        assert row["status"] == "in_progress"
        assert row["quarantine_reason"] is None
        assert row["owner_notification"] is None
        assert await real_redis.xlen(DEPLOY_QUEUE) == before_deploys
    finally:
        await stream.close()


@pytest.mark.asyncio
async def test_defensive_empty_success_uses_the_real_atomic_story_stop(real_redis, durable_api):
    from src.consumers.engineering_result_handler import (
        EngineeringSuccessParams,
        handle_engineering_success,
    )

    api, project_id, story_id, run_id = durable_api
    stream = RedisStreamClient()
    await stream.connect()
    try:
        with patch("src.consumers.engineering_result_handler.api_client", api):
            outcome = await handle_engineering_success(
                EngineeringSuccessParams(
                    result={"engineering_status": "done"},
                    task_id=run_id,
                    project=await api.get_project(project_id),
                    callback_stream=None,
                    redis=stream,
                    skip_deploy=False,
                    story_id=story_id,
                )
            )
        assert outcome["status"] == "failed"
        assert (
            await api.get_run(run_id)
        ).result.failure_reason is EngineeringFailureReason.NO_NEW_COMMIT
        row = await _durable_stop_row(story_id)
        assert row["status"] == "waiting_human_review"
        assert row["waiting_on"] == "human_review"
        assert row["quarantine_reason"]["code"] == "no_new_commit"
        assert row["owner_notification"]["state"] == "owed"
        assert row["owner_notification"]["admin_state"] == "owed"
    finally:
        await stream.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("planned,precommitted", [(False, False), (False, True), (True, False)])
@pytest.mark.parametrize("write_failure", ["before_commit", "after_commit", "persistent"])
async def test_transient_terminal_write_preserves_the_known_empty_attempt(  # noqa: PLR0915
    durable_api,
    real_redis,
    planned,
    precommitted,
    write_failure,
):
    from shared.contracts.dto.engineering_execution import EngineeringExecutionEvidence
    from shared.contracts.dto.story_failure import StoryFailure
    from src.clients.worker_spawner import SpawnResult
    from src.consumers.engineering import process_engineering_job
    from src.consumers.engineering_result_handler import EmptyResultSettlementError

    api, project_id, story_id, run_id = durable_api
    task = None
    if planned:
        task = await api.post(
            "tasks/",
            json={
                "project_id": project_id,
                "story_id": story_id,
                "title": "Last planned attempt",
                "status": "in_dev",
                "max_iterations": 3,
            },
        )
        await api.patch(f"tasks/{task['id']}", json={"current_iteration": 3})
        async with await AsyncConnection.connect(os.environ["TEST_DATABASE_URL"]) as db:
            await db.execute("UPDATE runs SET task_id=%s WHERE id=%s", (task["id"], run_id))
    if precommitted:
        await api.stop_story(
            story_id,
            "human-review",
            StoryFailure(
                code="no_new_commit",
                source="engineering",
                detail=f"Attempt {run_id}: Worker reported success but no commit was made",
            ),
            actor="engineering-worker",
        )
    original_row = await _durable_stop_row(story_id)
    original_patch = api.patch
    writes = []

    async def fail_first_write(path, *args, **kwargs):
        body = kwargs.get("json", {})
        if path == f"runs/{run_id}" and body.get("status") == "failed":
            writes.append(body)
            if write_failure == "persistent" or len(writes) == 1:
                if write_failure == "after_commit":
                    await original_patch(path, *args, **kwargs)
                raise httpx.ConnectError("Authorization: Bearer RUN_WRITE_CANARY")
        return await original_patch(path, *args, **kwargs)

    spawn = AsyncMock(
        return_value=SpawnResult(
            request_id="settled-empty-turn",
            success=True,
            exit_code=0,
            output="",
            turn_result_consumed=True,
            transcript_path="/fixture/empty-attempt.jsonl",
            execution=EngineeringExecutionEvidence(execution_phase="agent_started"),
        )
    )
    stream = RedisStreamClient()
    await stream.connect()
    before_deploys = await real_redis.xlen(DEPLOY_QUEUE)
    try:
        with (
            patch("src.consumers.engineering.api_client", api),
            patch("src.consumers.engineering_result_handler.api_client", api),
            patch("src.nodes.developer.api_client", api),
            patch(
                "src.nodes.developer.GitHubAppClient",
                return_value=await _github_reporting_a_deployed_head(),
            ),
            patch("src.consumers.engineering._resolve_allocations", AsyncMock(return_value={})),
            patch("src.consumers.engineering._build_story_context", AsyncMock(return_value=None)),
            patch("src.consumers.engineering._build_story_md", AsyncMock(return_value=None)),
            patch("src.nodes.developer.request_spawn", spawn),
            patch.object(api, "patch", side_effect=fail_first_write),
            patch.object(api, "stop_story", wraps=api.stop_story) as stops,
            patch(
                "src.consumers.engineering_result_handler.publish_worker_deletion", AsyncMock()
            ) as deletion,
            capture_logs() as logs,
        ):
            message = {
                **_engineering_message(),
                "task_id": run_id,
                "project_id": project_id,
                "story_id": story_id,
                "planning_task_id": task["id"] if planned else None,
            }
            if write_failure == "persistent":
                with pytest.raises(EmptyResultSettlementError):
                    await process_engineering_job(message, stream)
            else:
                outcome = await process_engineering_job(message, stream)
        assert len(writes) == 2
        assert writes[0] == writes[1]
        spawn.assert_awaited_once()
        deletion.assert_not_awaited()
        if precommitted:
            stops.assert_not_awaited()
        assert "RUN_WRITE_CANARY" not in json.dumps(logs)
        assert await real_redis.xlen(DEPLOY_QUEUE) == before_deploys
        if write_failure == "persistent":
            assert (await api.get_run(run_id)).status.value == "running"
            if planned:
                assert (await api.get_task(task["id"])).status.value == "in_dev"
            else:
                row = await _durable_stop_row(story_id)
                assert row["quarantine_reason"]["code"] == "no_new_commit"
                assert row["owner_notification"]["state"] == "owed"
                if precommitted:
                    assert row == original_row
            return
        assert outcome["status"] == "failed"
        run = await api.get_run(run_id)
        assert run.status.value == "failed"
        assert run.result.failure_reason is EngineeringFailureReason.NO_NEW_COMMIT
        assert run.result.execution.execution_phase.value == "agent_started"
        async with await AsyncConnection.connect(os.environ["TEST_DATABASE_URL"]) as db:
            cursor = await db.execute(
                "SELECT count(*) FROM engineering_attempt_ledger WHERE run_id=%s", (run_id,)
            )
            assert (await cursor.fetchone())[0] == 1
        assert (await api.get(f"runs/{run_id}"))[
            "transcript_path"
        ] == "/fixture/empty-attempt.jsonl"
        row = await _durable_stop_row(story_id)
        if planned:
            assert row == original_row
            assert (await api.get_task(task["id"])).status.value == "failed"
            # Run the actual scheduler in its own import/process boundary against
            # the same API, PostgreSQL and Redis, using the persisted producer result.
            env = os.environ | {
                "PYTHONPATH": "/app/scheduler:/app",
                "API_BASE_URL": os.environ["TEST_API_BASE_URL"],
                "SETTLEMENT_TASK_ID": task["id"],
            }
            completed = subprocess.run(
                [
                    sys.executable,
                    "-P",
                    "-c",
                    """
import asyncio
import os
from src.clients.api import SchedulerAPIClient
from src.tasks.supervisor.liveness import supervise_failed_tasks
from shared.redis import RedisStreamClient

async def settle():
    api = SchedulerAPIClient()
    stream = RedisStreamClient()
    await stream.connect()
    try:
        await supervise_failed_tasks(api, stream)
        task = await api.get_task(os.environ['SETTLEMENT_TASK_ID'])
        assert task.status.value == 'waiting_human_review'
    finally:
        await stream.close()
        await api.close()

asyncio.run(settle())
""",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert completed.returncode == 0, completed.stdout + completed.stderr
            stopped = await _durable_stop_row(story_id)
            assert stopped["quarantine_reason"]["code"] == "no_new_commit"
            assert run_id in stopped["quarantine_reason"]["detail"]
            assert stopped["owner_notification"]["state"] == "owed"
            assert stopped["owner_notification"]["admin_state"] == "owed"
        else:
            assert row["quarantine_reason"]["code"] == "no_new_commit"
            assert row["owner_notification"]["state"] == "owed"
            assert row["owner_notification"]["admin_state"] == "owed"
            if precommitted:
                assert row == original_row
    finally:
        await stream.close()
