"""Resolver/consumer and merged repair cross the real API, PostgreSQL and Redis."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
import json
import os
from pathlib import Path
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

from shared.contracts.queues.deploy import DeployMessage, DeployOutcome
from shared.contracts.queues.po import unprotect_po_payload
from shared.queues import DEPLOY_QUEUE, PO_INPUT_QUEUE
from shared.redis import RedisStreamClient
from src.clients.api import LanggraphAPIClient
from src.consumers.deploy import _route_deploy_result
from src.deploy_fence import DeployFence
from src.subgraphs.devops.deployer import DeployerNode
from src.subgraphs.devops.graph import create_devops_subgraph
from tests.service._public_deploy_transport import ContentGitHubFixture, released_workflow

HEAD = "a" * 40
BUILT = "e" * 40
FIX_HEAD = "b" * 40
FIX_BUILT = "f" * 40
UNKNOWN_KEY = "UNKNOWN_REQUIRED_SELF_ADDRESS"
CANARY = "123456789:AA-public-deploy-secret-canary"


@asynccontextmanager
async def held_deploy_lock(stream, project_id, task_id):
    """This deploy's claim on its project's deploy lock, held on the service Redis."""
    fence = DeployFence.for_job(stream.redis, project_id, task_id)
    assert await fence.acquire(60)
    try:
        yield fence
    finally:
        await fence.release()


async def delete_public_project(api, project_id):
    try:
        async with await AsyncConnection.connect(os.environ["TEST_DATABASE_URL"]) as db:
            await db.execute(
                "DELETE FROM users_grant_intents WHERE project_id=%s", (uuid.UUID(project_id),)
            )
    finally:
        await api.delete(f"projects/{project_id}")


@asynccontextmanager
async def public_project_context():
    # Register cleanup as resources are acquired, including a failed setup.
    async with AsyncExitStack() as stack:
        api = LanggraphAPIClient()
        stack.push_async_callback(api.close)
        api.base_url = os.environ["TEST_API_BASE_URL"]
        telegram_id = uuid.uuid4().int % 1_000_000_000
        await api.post(
            "users/", json={"telegram_id": telegram_id, "username": f"public_{telegram_id}"}
        )
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
        stack.push_async_callback(delete_public_project, api, project_id)
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
        stack.push_async_callback(stream.close)
        await stream.connect()
        yield api, stream, project_id, story["id"]


@pytest.fixture
async def public_project():
    async with public_project_context() as project:
        yield project


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
@pytest.mark.parametrize(
    "cause",
    [UNKNOWN_KEY, "::ffff:127.0.0.1", "::ffff:0.0.0.0", "::ffff:224.0.0.1", "unupgraded_workflow"],
)
async def test_unknown_resolver_cause_survives_consumer_and_transactional_story_stop(
    public_project, real_redis, null_result, cause
):
    api, stream, project_id, story_id = public_project
    await api.transition_story(story_id, "deploy")
    run_id = "deploy-public-" + uuid.uuid4().hex
    await api.post(
        "runs/",
        json={"id": run_id, "type": "deploy", "project_id": project_id, "story_id": story_id},
    )
    failed_key = UNKNOWN_KEY if cause == UNKNOWN_KEY else "PUBLIC_BASE_URL"
    visible_name = ".github/workflows/deploy.yml" if cause == "unupgraded_workflow" else failed_key
    contract = {
        "version": "1",
        "entries": {
            failed_key: {"source": "derived", "required": True, "environments": ["production"]},
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
    if cause.startswith("::ffff:"):
        state["allocated_resources"]["backend"]["server_ip"] = cause
    if cause == "unupgraded_workflow":
        state["allocated_resources"]["backend"]["server_ip"] = "2001:db8::42"
        del contract["entries"]["USERS_GRANT_CAPABILITY"]
    github = ContentGitHubFixture(
        BUILT, released_workflow().replace('"$SCP_HOST:$TARGET/"', '"$HOST:$TARGET/"')
    )
    deployment = AsyncMock()
    with (
        patch(
            "src.subgraphs.devops.env_contract_loader._fetch_env_contract",
            AsyncMock(return_value=contract),
        ),
        patch("src.subgraphs.devops.deployer.GitHubAppClient", return_value=github),
        patch("src.consumers.deploy_failure_handler.api_client", api),
        capture_logs() as logs,
    ):
        if cause == "unupgraded_workflow":
            result = await create_devops_subgraph().ainvoke(state)
            assert github.reads == [
                ("/repos/fixture/public/contents/.github/workflows/deploy.yml", BUILT)
            ]
            github.set_repository_secrets.assert_not_awaited()
            github.create_or_reset_tag.assert_not_awaited()
            github.trigger_workflow_dispatch.assert_not_awaited()
        else:
            with patch("src.subgraphs.devops.graph.deployer_node.run", deployment):
                result = await create_devops_subgraph().ainvoke(state)
        assert result["resolution_outcome"] is DeployOutcome.ENVIRONMENT_RESOLUTION_FAILED
        deployment.assert_not_awaited()
        # A diagnostic echo at the consumer boundary cannot become stored/user text.
        result["errors"].append(f"Authorization: Bearer {CANARY} " + "diagnostic " * 100)
        async with held_deploy_lock(stream, project_id, run_id) as fence:
            outcome = await _route_deploy_result(
                result,
                SimpleNamespace(),
                DeployMessage(
                    task_id=run_id,
                    project_id=project_id,
                    story_id=story_id,
                    telegram_chat_id="123",
                ),
                stream,
                fence,
            )
        assert outcome["status"] == "failed"
    run = await api.get(f"runs/{run_id}")
    assert run["status"] == "failed"
    assert run["result"]["deploy_outcome"] == "environment_resolution_failed"
    await github.close()
    assert visible_name in run["result"]["error_details"]
    assert len(run["result"]["error_details"]) <= 503
    assert CANARY not in json.dumps(run) + json.dumps(logs)
    scheduler("refuse", story_id)
    assert (await story_row(story_id))["quarantine_reason"] is None
    before = await real_redis.xlen(PO_INPUT_QUEUE)
    scheduler("fail", story_id)
    row = await story_row(story_id)
    assert row["status"] == "failed"
    assert row["quarantine_reason"]["code"] == "environment_resolution_failed"
    assert visible_name in row["quarantine_reason"]["detail"]
    assert row["owner_notification"]["state"] == "owed"
    assert row["owner_notification"]["admin_state"] == "owed"
    assert await real_redis.xlen(PO_INPUT_QUEUE) == before
    scheduler("interrupt_notify", story_id)
    interrupted = await story_row(story_id)
    assert interrupted["owner_notification"]["state"] == "owed"
    assert interrupted["owner_notification"]["attempts"] == 1
    assert visible_name in interrupted["owner_notification"]["text"]
    async with await AsyncConnection.connect(os.environ["TEST_DATABASE_URL"]) as db:
        await db.execute(
            "UPDATE stories SET owner_notification=jsonb_set(owner_notification::jsonb, "
            "'{last_attempt_at}', to_jsonb(%s::text))::json WHERE id=%s",
            ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), story_id),
        )
    scheduler("notify", story_id)
    entries = await real_redis.xrevrange(PO_INPUT_QUEUE, count=10)
    decoded = [unprotect_po_payload(PO_INPUT_QUEUE, fields) for _, fields in entries]
    assert any(visible_name in json.dumps(item) for item in decoded)
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
    "mode", ["missing", "unreadable", "comment", "unselected", "custom", "rejected", "wrong_ref"]
)
async def test_authenticated_built_workflow_read_refuses_before_external_effects(
    public_project, mode
):
    _, _, project_id, _ = public_project
    source = released_workflow()
    rejected = None
    if mode == "missing":
        source = None
    elif mode == "unreadable":
        source = RuntimeError(CANARY)
    elif mode == "comment":
        source = "\n".join("# " + line for line in source.splitlines()) + "\njobs: {}\n"
    elif mode == "unselected":
        source = source.replace(
            "      - name: Copy compose files to server",
            "      - if: false\n        name: Copy compose files to server",
        )
    elif mode == "custom":
        source = source.replace("scp $SSH_OPTS", "scp -O $SSH_OPTS")
    elif mode == "rejected":
        rejected = "+ runs-on: self-hosted"
    else:
        # A corrected head/default branch is no evidence for this old built tree.
        source = source.replace('"$SCP_HOST:$TARGET/"', '"$HOST:$TARGET/"')
    github = ContentGitHubFixture(BUILT, source, rejected)
    state = {
        "project_id": project_id,
        "project_spec": {"slug": "public-test", "config": {"modules": ["backend"]}},
        "repo_info": {"html_url": "https://github.com/fixture/public"},
        "head_sha": HEAD,
        "deployed_commit_sha": BUILT,
        "allocated_resources": {
            "backend": {"service_name": "backend", "server_ip": "2001:db8::42", "port": 8080}
        },
        "secret_values": {"TOKEN": CANARY},
        "fence_active_deploys": True,
    }
    try:
        with (
            patch("src.subgraphs.devops.deployer.GitHubAppClient", return_value=github),
            capture_logs() as logs,
        ):
            result = await DeployerNode().run(state)
        assert result["resolution_outcome"] is DeployOutcome.ENVIRONMENT_RESOLUTION_FAILED
        assert "DEPLOY_HOST" in " ".join(result["errors"])
        assert CANARY not in json.dumps(result) + json.dumps(logs)
        assert github.reads[0] == (
            "/repos/fixture/public/contents/.github/workflows/deploy.yml",
            BUILT,
        )
        for name in (
            "set_repository_secrets",
            "create_or_reset_tag",
            "trigger_workflow_dispatch",
            "rerun_failed_jobs",
            "fence_workflow",
        ):
            getattr(github, name).assert_not_awaited()
    finally:
        await github.close()


def merged_updated_workflow(tmp_path):
    """A normal local reviewed product merge provides distinct real head/built SHAs."""
    product = tmp_path / "reviewed-product"
    product.mkdir()
    workflow = product / ".github/workflows/deploy.yml"
    workflow.parent.mkdir(parents=True)
    source = released_workflow()
    workflow.write_text(source.replace('"$SCP_HOST:$TARGET/"', '"$HOST:$TARGET/"'))

    def git(*args):
        result = subprocess.run(
            ["git", "-c", "user.name=Product tests", "-c", "user.email=tests@example.com", *args],
            cwd=product,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.strip()

    git("init", "--quiet", "-b", "main")
    git("add", "-A")
    git("commit", "--quiet", "-m", "Previous transport")
    git("switch", "-c", "reviewed-update")
    workflow.write_text(source.replace("name: Deploy", "name: Owned deploy", 1))
    git("add", "-A")
    git("commit", "--quiet", "-m", "Reviewed released executable transport")
    head = git("rev-parse", "HEAD")
    git("switch", "main")
    git("merge", "--no-ff", "reviewed-update", "-m", "Merge reviewed product update")
    built = git("rev-parse", "HEAD")
    assert head != built
    assert git("status", "--porcelain") == ""
    return (
        head,
        built,
        subprocess.check_output(
            ["git", "show", f"{built}:.github/workflows/deploy.yml"], cwd=product, text=True
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("address", "expected_host"),
    [("2001:db8::42", "2001:db8::42"), ("::ffff:192.0.2.42", "::ffff:c000:22a")],
)
async def test_reviewed_updated_merge_recovers_exhausted_intent_and_finishes_smoke(  # noqa: PLR0915
    public_project, real_redis, tmp_path, address, expected_host
):
    api, stream, project_id, story_id = public_project
    await api.post(
        "system-configs/",
        json={"key": "deploy.max_deploy_retries", "value": 1, "category": "deploy"},
    )
    old_attempt = await lifecycle(api, project_id, story_id)
    await api.transition_story(story_id, "deploy")
    old_run_id = old_attempt["execution_run_id"]
    old_state = {
        "project_id": project_id,
        "project_spec": {"slug": "public-test", "config": {"modules": ["backend"]}},
        "repo_info": {"html_url": "https://github.com/fixture/public"},
        "head_sha": HEAD,
        "deployed_commit_sha": BUILT,
        "allocated_resources": {
            "backend": {"service_name": "backend", "server_ip": address, "port": 8080}
        },
        "errors": [],
        "messages": [],
    }
    old_contract = {
        "version": "1",
        "entries": {
            "PUBLIC_BASE_URL": {
                "source": "derived",
                "required": True,
                "environments": ["production"],
            }
        },
    }
    old_github = ContentGitHubFixture(
        BUILT, released_workflow().replace('"$SCP_HOST:$TARGET/"', '"$HOST:$TARGET/"')
    )
    try:
        with (
            patch(
                "src.subgraphs.devops.env_contract_loader._fetch_env_contract",
                AsyncMock(return_value=old_contract),
            ),
            patch("src.subgraphs.devops.deployer.GitHubAppClient", return_value=old_github),
            patch("src.consumers.deploy_failure_handler.api_client", api),
        ):
            refused = await create_devops_subgraph().ainvoke(old_state)
            assert refused["resolution_outcome"] is DeployOutcome.ENVIRONMENT_RESOLUTION_FAILED
            async with held_deploy_lock(stream, project_id, old_run_id) as fence:
                await _route_deploy_result(
                    refused,
                    SimpleNamespace(),
                    DeployMessage(
                        task_id=old_run_id,
                        project_id=project_id,
                        story_id=story_id,
                        telegram_chat_id="123",
                    ),
                    stream,
                    fence,
                )
    finally:
        await old_github.close()
    terminal = await lifecycle(api, project_id, story_id)
    assert terminal["disposition"] == "exhausted"
    intent_path = f"projects/{project_id}/users/grant-intents/{terminal['intent_id']}"
    previous = await api.get(intent_path)
    scheduler("fail", story_id)
    stopped = await story_row(story_id)
    assert ".github/workflows/deploy.yml" in stopped["quarantine_reason"]["detail"]
    assert "DEPLOY_HOST" in stopped["owner_notification"]["text"]
    for action in ("reopen", "start", "pr_review"):
        await api.transition_story(story_id, action)
    await api.patch(f"stories/{story_id}", json={"pr_number": 42})
    previous_run = await api.get(f"runs/{previous['execution_run_id']}")
    head, built, source = merged_updated_workflow(tmp_path)
    scheduler(
        "poll", story_id, PUBLIC_DEPLOY_PR="42", PUBLIC_DEPLOY_HEAD=head, PUBLIC_DEPLOY_BUILT=built
    )
    intent = await api.get(intent_path)
    assert intent["id"] == previous["id"] and intent["attempts"] == 1
    assert intent["target_history"][0]["sha"] == HEAD
    assert intent["target_history"][0]["attempts"] == 1
    run_id = intent["execution_run_id"]
    run = await api.get(f"runs/{run_id}")
    assert run["story_id"] == story_id
    assert run["run_metadata"]["head_sha"] == head
    assert run["run_metadata"]["deployed_commit_sha"] == built
    entries = await real_redis.xrange(DEPLOY_QUEUE)
    assert sum(json.loads(fields[b"data"])["task_id"] == run_id for _, fields in entries) == 1
    contract = {
        "version": "1",
        "entries": {
            key: {"source": "derived", "required": True, "environments": ["production"]}
            for key in ("PUBLIC_BASE_URL", "BACKEND_IMAGE")
        },
    }
    state = {
        "project_id": project_id,
        "project_spec": {"slug": "public-test", "config": {"modules": ["backend"]}},
        "repo_info": {"html_url": "https://github.com/fixture/public"},
        "head_sha": head,
        "deployed_commit_sha": built,
        "allocated_resources": {
            "backend": {
                "service_name": "backend",
                "server_ip": address,
                "port": 8080,
                "server_handle": "synthetic-server",
            }
        },
        "errors": [],
        "messages": [],
    }
    fence = DeployFence.for_job(stream.redis, project_id, run_id)
    assert await fence.acquire(60)
    state["deploy_fence"] = fence
    github = ContentGitHubFixture(built, source)
    github.set_repository_secrets.return_value = 9
    github.wait_for_workflow_completion = AsyncMock(
        return_value={"id": 1439, "status": "completed", "conclusion": "success", "head_sha": built}
    )
    requests = []
    client = httpx.AsyncClient

    def healthy(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "ok"})

    try:
        with (
            patch(
                "src.subgraphs.devops.env_contract_loader._fetch_env_contract",
                AsyncMock(return_value=contract),
            ),
            patch("src.subgraphs.devops.deployer.GitHubAppClient", return_value=github),
            patch(
                "src.subgraphs.devops.deployer.verify_published_images",
                AsyncMock(return_value={"BACKEND_IMAGE": "sha256:" + "d" * 64}),
            ),
            patch(
                "src.subgraphs.devops.deployer.DeployerNode._server_credentials",
                AsyncMock(return_value=(SimpleNamespace(ssh_user="test"), "synthetic-key")),
            ),
            patch(
                "src.subgraphs.devops.deployer._create_deployment_record", AsyncMock(return_value=1)
            ),
            patch.dict(
                os.environ,
                {
                    "ORCHESTRATOR_HOSTNAME": "registry.test",
                    "REGISTRY_USER": "fixture",
                    "REGISTRY_PASSWORD": "fixture",
                },
            ),
            patch(
                "src.subgraphs.devops.smoke.httpx.AsyncClient",
                side_effect=lambda: client(transport=httpx.MockTransport(healthy)),
            ),
        ):
            result = await create_devops_subgraph().ainvoke(state)
        assert result["smoke_result"]["status"] == "pass" and not result["errors"]
        assert len(requests) == 1
        assert str(requests[0].url) == result["non_secret_values"]["PUBLIC_BASE_URL"] + "/health"
        assert result["deployed_url"] == result["non_secret_values"]["PUBLIC_BASE_URL"]
        assert requests[0].url.host == httpx.URL(result["deployed_url"]).host
        assert requests[0].url.host == expected_host and ip_address(
            requests[0].url.host
        ) == ip_address(address)
        assert github.reads == [
            ("/repos/fixture/public/contents/.github/workflows/deploy.yml", built),
            ("/repos/fixture/public/contents/.github/workflows/deploy.yml.rej", built),
        ]
        github.trigger_workflow_dispatch.assert_awaited_once()
        github.delete_ref.assert_awaited_once()
        assert await api.get(f"runs/{previous['execution_run_id']}") == previous_run
    finally:
        await github.close()
        await fence.release()


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
