"""Resolver/consumer and merged repair cross the real API, PostgreSQL and Redis."""

import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

from psycopg import AsyncConnection
from psycopg.rows import dict_row
import pytest
from structlog.testing import capture_logs

from shared.contracts.queues.deploy import DeployMessage, DeployOutcome
from shared.contracts.queues.po import unprotect_po_payload
from shared.queues import DEPLOY_QUEUE, PO_INPUT_QUEUE
from shared.redis import RedisStreamClient
from src.clients.api import LanggraphAPIClient
from src.consumers.deploy import _route_deploy_result
from src.subgraphs.devops.graph import create_devops_subgraph

HEAD = "a" * 40
BUILT = "e" * 40
FIX_HEAD = "b" * 40
FIX_BUILT = "f" * 40
UNKNOWN_KEY = "UNKNOWN_REQUIRED_SELF_ADDRESS"
CANARY = "123456789:AA-public-deploy-secret-canary"


@pytest.fixture
async def public_project():
    api = LanggraphAPIClient()
    api.base_url = os.environ["TEST_API_BASE_URL"]
    telegram_id = uuid.uuid4().int % 1_000_000_000
    await api.post("users/", json={"telegram_id": telegram_id, "username": f"public_{telegram_id}"})
    project = await api.post(
        "projects/",
        json={
            "title": "Public deploy",
            "initiating_run_id": "public-fixture",
            "config": {"modules": ["backend", "tg_bot"]},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    project_id = project["id"]
    await api.post(
        "repositories/",
        json={
            "project_id": project_id,
            "name": "public",
            "git_url": "https://github.com/fixture/public.git",
            "role": "primary",
        },
    )
    story = await api.post(
        "stories/", json={"project_id": project_id, "title": "Public deployment"}
    )
    await api.transition_story(story["id"], "start")
    await api.transition_story(story["id"], "pr_review")
    await api.patch(f"stories/{story['id']}", json={"pr_number": 42})
    stream = RedisStreamClient()
    await stream.connect()
    try:
        yield api, stream, project_id, story["id"]
    finally:
        async with await AsyncConnection.connect(os.environ["TEST_DATABASE_URL"]) as db:
            await db.execute(
                "DELETE FROM users_grant_intents WHERE project_id=%s", (uuid.UUID(project_id),)
            )
        await api.delete(f"projects/{project_id}")
        await api.close()
        await stream.close()


def scheduler(mode, story_id, **extra):
    env = os.environ | {
        "PYTHONPATH": "/app/scheduler:/app",
        "API_BASE_URL": os.environ["TEST_API_BASE_URL"],
        "PUBLIC_DEPLOY_MODE": mode,
        "PUBLIC_DEPLOY_STORY": story_id,
        **extra,
    }
    completed = subprocess.run(
        [sys.executable, "-P", str(Path(__file__).with_name("_public_deploy_scheduler.py"))],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert CANARY not in completed.stdout + completed.stderr


async def story_row(story_id):
    async with await AsyncConnection.connect(
        os.environ["TEST_DATABASE_URL"], row_factory=dict_row
    ) as db:
        cursor = await db.execute(
            "SELECT status, quarantine_reason, owner_notification FROM stories WHERE id=%s",
            (story_id,),
        )
        return await cursor.fetchone()


@pytest.mark.asyncio
@pytest.mark.parametrize("null_result", [False, True])
async def test_unknown_resolver_cause_survives_consumer_and_transactional_story_stop(
    public_project, real_redis, null_result
):
    api, stream, project_id, story_id = public_project
    await api.transition_story(story_id, "deploy")
    run_id = "deploy-public-" + uuid.uuid4().hex
    await api.post(
        "runs/",
        json={"id": run_id, "type": "deploy", "project_id": project_id, "story_id": story_id},
    )
    contract = {
        "version": "1",
        "entries": {
            UNKNOWN_KEY: {"source": "derived", "required": True, "environments": ["production"]},
            "USERS_GRANT_CAPABILITY": {
                "source": "generated_secret",
                "required": True,
                "environments": ["production"],
            },
        },
    }
    state = {
        "project_id": project_id,
        "project_spec": {"slug": "public-test", "config": {}},
        "repo_info": {"html_url": "https://github.com/fixture/public"},
        "head_sha": HEAD,
        "deployed_commit_sha": BUILT,
        "allocated_resources": {
            "backend": {"service_name": "backend", "server_ip": "192.0.2.42", "port": 8080}
        },
        "errors": [],
        "messages": [],
        "provided_secrets": {"TOKEN": CANARY},
    }
    if null_result:
        state["deployment_result"] = None
    deployment = AsyncMock()
    with (
        patch(
            "src.subgraphs.devops.env_contract_loader._fetch_env_contract",
            AsyncMock(return_value=contract),
        ),
        patch("src.subgraphs.devops.graph.deployer_node.run", deployment),
        patch("src.consumers.deploy_failure_handler.api_client", api),
        capture_logs() as logs,
    ):
        result = await create_devops_subgraph().ainvoke(state)
        assert result["resolution_outcome"] is DeployOutcome.ENVIRONMENT_RESOLUTION_FAILED
        deployment.assert_not_awaited()
        # A diagnostic echo at the consumer boundary cannot become stored/user text.
        result["errors"].append(f"Authorization: Bearer {CANARY} " + "diagnostic " * 100)
        outcome = await _route_deploy_result(
            result,
            SimpleNamespace(),
            DeployMessage(
                task_id=run_id, project_id=project_id, story_id=story_id, telegram_chat_id="123"
            ),
            stream,
        )
        assert outcome["status"] == "failed"
    run = await api.get(f"runs/{run_id}")
    assert run["status"] == "failed"
    assert run["result"]["deploy_outcome"] == "environment_resolution_failed"
    assert UNKNOWN_KEY in run["result"]["error_details"]
    assert len(run["result"]["error_details"]) <= 503
    assert CANARY not in json.dumps(run) + json.dumps(logs)
    scheduler("refuse", story_id)
    assert (await story_row(story_id))["quarantine_reason"] is None
    before = await real_redis.xlen(PO_INPUT_QUEUE)
    scheduler("fail", story_id)
    row = await story_row(story_id)
    assert row["status"] == "failed"
    assert row["quarantine_reason"]["code"] == "environment_resolution_failed"
    assert UNKNOWN_KEY in row["quarantine_reason"]["detail"]
    assert row["owner_notification"]["state"] == "owed"
    assert row["owner_notification"]["admin_state"] == "owed"
    assert await real_redis.xlen(PO_INPUT_QUEUE) == before
    scheduler("interrupt_notify", story_id)
    interrupted = await story_row(story_id)
    assert interrupted["owner_notification"]["state"] == "owed"
    assert interrupted["owner_notification"]["attempts"] == 1
    assert UNKNOWN_KEY in interrupted["owner_notification"]["text"]
    async with await AsyncConnection.connect(os.environ["TEST_DATABASE_URL"]) as db:
        await db.execute(
            "UPDATE stories SET owner_notification=jsonb_set(owner_notification::jsonb, "
            "'{last_attempt_at}', to_jsonb(%s::text))::json WHERE id=%s",
            ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), story_id),
        )
    scheduler("notify", story_id)
    entries = await real_redis.xrevrange(PO_INPUT_QUEUE, count=10)
    decoded = [unprotect_po_payload(PO_INPUT_QUEUE, fields) for _, fields in entries]
    assert any(UNKNOWN_KEY in json.dumps(item) for item in decoded)
    assert CANARY not in json.dumps(row) + json.dumps(decoded)


async def lifecycle(api, project_id, story_id, head=HEAD, built=BUILT, **extra):
    return await api.post(
        f"projects/{project_id}/users/grant-intents/lifecycle",
        json={
            "kind": "initial_owner",
            "story_id": story_id,
            "head_sha": head,
            "deployed_commit_sha": built,
            **extra,
        },
    )


@pytest.mark.asyncio
async def test_current_merged_fix_opens_one_bounded_epoch_after_committed_exhaustion(
    public_project, real_redis
):
    api, _, project_id, story_id = public_project
    await api.post(
        "system-configs/",
        json={"key": "deploy.max_deploy_retries", "value": 2, "category": "deploy"},
    )
    prior_runs = []
    for _ in range(2):
        attempt = await lifecycle(api, project_id, story_id)
        assert attempt["disposition"] == "dispatched"
        prior_runs.append(attempt["execution_run_id"])
        await api.patch(
            f"runs/{attempt['execution_run_id']}",
            json={
                "status": "failed",
                "result": {
                    "deploy_outcome": "environment_resolution_failed",
                    "error_details": UNKNOWN_KEY,
                },
            },
        )
    exhausted = await lifecycle(api, project_id, story_id)
    assert exhausted["disposition"] == "exhausted"
    intent_path = f"projects/{project_id}/users/grant-intents/{exhausted['intent_id']}"
    original = await api.get(intent_path)
    assert original["status"] == "failed"
    assert original["attempts"] == 2
    snapshots = [await api.get(f"runs/{run_id}") for run_id in prior_runs]
    arbitrary = await lifecycle(api, project_id, story_id, FIX_HEAD, FIX_BUILT)
    assert arbitrary["disposition"] == "exhausted"
    scheduler(
        "poll",
        story_id,
        PUBLIC_DEPLOY_PR="42",
        PUBLIC_DEPLOY_HEAD=FIX_HEAD,
        PUBLIC_DEPLOY_BUILT=FIX_BUILT,
    )
    intent = await api.get(intent_path)
    assert intent["id"] == original["id"]
    assert intent["target_sha"] == FIX_HEAD
    assert intent["attempts"] == 1
    assert len(intent["target_history"]) == 1
    assert intent["target_history"][0]["sha"] == HEAD
    assert intent["target_history"][0]["attempts"] == 2
    assert intent["retry_history"] == original["retry_history"]
    run_id = intent["execution_run_id"]
    run = await api.get(f"runs/{run_id}")
    assert run_id not in prior_runs
    assert run["story_id"] == story_id
    assert run["run_metadata"]["head_sha"] == FIX_HEAD
    assert run["run_metadata"]["deployed_commit_sha"] == FIX_BUILT
    entries = await real_redis.xrange(DEPLOY_QUEUE)
    messages = [json.loads(fields[b"data"]) for _, fields in entries]
    queued = [message for message in messages if message["task_id"] == run_id]
    assert len(queued) == 1
    assert queued[0]["head_sha"] == FIX_HEAD
    assert queued[0]["deployed_commit_sha"] == FIX_BUILT
    assert queued[0]["story_id"] == story_id
    repeated = await lifecycle(api, project_id, story_id, FIX_HEAD, FIX_BUILT, merged_pr_number=42)
    assert repeated["disposition"] == "in_flight"
    await api.patch(f"runs/{run_id}", json={"status": "failed"})
    second = await lifecycle(api, project_id, story_id, FIX_HEAD, FIX_BUILT)
    assert second["disposition"] == "dispatched"
    await api.patch(f"runs/{second['execution_run_id']}", json={"status": "failed"})
    terminal = await lifecycle(api, project_id, story_id, FIX_HEAD, FIX_BUILT, merged_pr_number=42)
    assert terminal["disposition"] == "exhausted"
    assert snapshots == [await api.get(f"runs/{run_id}") for run_id in prior_runs]


async def merged_evidence(api, project_id, story_id):
    return {
        "pull_request": {
            "number": 42,
            "state": "closed",
            "merged_at": datetime.now(UTC).isoformat(),
            "head_sha": FIX_HEAD,
            "merge_commit_sha": FIX_BUILT,
        },
        "latest_ci_observation": {
            "ci_run_id": 12345,
            "ci_status": "completed",
            "ci_conclusion": "success",
        },
        "ci_runs": [
            {
                "id": 12345,
                "branch": "main",
                "head_sha": FIX_BUILT,
                "status": "completed",
                "conclusion": "success",
            }
        ],
        "deploy_observation": {
            "story_id": story_id,
            "project_id": project_id,
            "repository_url": "https://github.com/fixture/public.git",
            "observed_at": datetime.now(UTC).isoformat(),
        },
    }


async def exhausted_epoch(api, project_id, story_id):
    await api.post(
        "system-configs/",
        json={"key": "deploy.max_deploy_retries", "value": 1, "category": "deploy"},
    )
    attempt = await lifecycle(api, project_id, story_id)
    await api.patch(f"runs/{attempt['execution_run_id']}", json={"status": "failed"})
    exhausted = await lifecycle(api, project_id, story_id)
    assert exhausted["disposition"] == "exhausted"
    path = f"projects/{project_id}/users/grant-intents/{exhausted['intent_id']}"
    return path, await api.get(path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch",
    [
        "missing",
        "pr",
        "head",
        "merge",
        "project",
        "story",
        "repository",
        "unpublished",
        "ci_head",
        "ci_branch",
        "ci_status",
        "old_cycle",
        "reopened",
        "future",
        "malformed",
        "request_pr",
        "request_story",
        "request_head",
        "request_merge",
    ],
)
async def test_exhausted_epoch_rejects_inconsistent_merge_evidence(
    public_project, real_redis, mismatch
):
    import httpx

    api, _, project_id, story_id = public_project
    path, before = await exhausted_epoch(api, project_id, story_id)
    evidence = await merged_evidence(api, project_id, story_id)
    request = {"story_id": story_id, "head": FIX_HEAD, "built": FIX_BUILT, "merged_pr_number": 42}
    mutations = {
        "pr": (evidence["pull_request"], "number", 41),
        "head": (evidence["pull_request"], "head_sha", "c" * 40),
        "merge": (evidence["pull_request"], "merge_commit_sha", BUILT),
        "project": (evidence["deploy_observation"], "project_id", str(uuid.uuid4())),
        "story": (evidence["deploy_observation"], "story_id", "old-story"),
        "repository": (
            evidence["deploy_observation"],
            "repository_url",
            "https://github.com/other/public.git",
        ),
        "unpublished": (evidence["latest_ci_observation"], "ci_conclusion", "failure"),
        "ci_head": (evidence["ci_runs"][0], "head_sha", BUILT),
        "ci_branch": (evidence["ci_runs"][0], "branch", "story/old"),
        "ci_status": (evidence["ci_runs"][0], "status", "in_progress"),
        "old_cycle": (
            evidence["pull_request"],
            "merged_at",
            (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        ),
        "future": (
            evidence["deploy_observation"],
            "observed_at",
            (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        ),
        "malformed": (evidence, "pull_request", ["broken"]),
        "request_pr": (request, "merged_pr_number", 41),
        "request_head": (request, "head", "c" * 40),
        "request_merge": (request, "built", "d" * 40),
    }
    if mismatch in mutations:
        mapping, key, value = mutations[mismatch]
        mapping[key] = value
    elif mismatch == "missing":
        evidence = {}
    elif mismatch == "reopened":
        for action in ("fail", "reopen", "start", "pr_review"):
            await api.transition_story(story_id, action)
    elif mismatch == "request_story":
        other = await api.post("stories/", json={"project_id": project_id, "title": "Other cycle"})
        request["story_id"] = other["id"]
    else:
        raise AssertionError(f"unknown evidence case {mismatch}")
    await api.patch(f"stories/{story_id}", json={"generated_product_timeline": evidence})
    queue_size = await real_redis.xlen(DEPLOY_QUEUE)
    with pytest.raises(httpx.HTTPStatusError) as refused:
        await lifecycle(api, project_id, **request)
    assert refused.value.response.status_code == 409
    assert "merged repair" in refused.value.response.json()["detail"]
    assert await api.get(path) == before
    assert await real_redis.xlen(DEPLOY_QUEUE) == queue_size


@pytest.mark.asyncio
async def test_concurrent_merged_repair_admissions_publish_one_live_run(public_project, real_redis):
    api, _, project_id, story_id = public_project
    path, before = await exhausted_epoch(api, project_id, story_id)
    await api.patch(
        f"stories/{story_id}",
        json={"generated_product_timeline": await merged_evidence(api, project_id, story_id)},
    )
    results = await asyncio.gather(
        *[
            lifecycle(api, project_id, story_id, FIX_HEAD, FIX_BUILT, merged_pr_number=42)
            for _ in range(8)
        ]
    )
    assert [result["disposition"] for result in results].count("dispatched") == 1
    assert [result["disposition"] for result in results].count("in_flight") == 7
    intent = await api.get(path)
    assert intent["attempts"] == 1
    assert intent["target_history"][0]["attempts"] == before["attempts"]
    run_id = intent["execution_run_id"]
    entries = await real_redis.xrange(DEPLOY_QUEUE)
    assert sum(json.loads(fields[b"data"])["task_id"] == run_id for _, fields in entries) == 1
    async with await AsyncConnection.connect(os.environ["TEST_DATABASE_URL"]) as db:
        cursor = await db.execute(
            "SELECT count(*) FROM runs WHERE project_id=%s AND status='queued'",
            (uuid.UUID(project_id),),
        )
        assert (await cursor.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_changed_source_run_metadata_cannot_replace_an_ordinary_terminal_target(
    public_project,
):
    api, _, project_id, story_id = public_project
    await api.post(
        "system-configs/",
        json={"key": "deploy.max_deploy_retries", "value": 2, "category": "deploy"},
    )
    first = await lifecycle(api, project_id, story_id)
    await api.patch(f"runs/{first['execution_run_id']}", json={"status": "failed"})
    path = f"projects/{project_id}/users/grant-intents/{first['intent_id']}"
    before = await api.get(path)
    refused = await lifecycle(api, project_id, story_id, FIX_HEAD, FIX_BUILT)
    assert refused["disposition"] == "stale_target"
    assert await api.get(path) == before
