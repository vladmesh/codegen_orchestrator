"""CI-only native consumer, exhaustion/notice and registered PO retry proof.

PostgreSQL and Redis are real. PR/image, fleet-readiness and administrator
delivery observations are synthetic. There is no live deployment or worker.
"""

import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from shared.contracts.dto.run_result import AllocationFailureReason
from shared.contracts.queues.deploy import DeployMessage, DeployOutcome
from shared.contracts.queues.po import unprotect_po_payload
from shared.queues import DEPLOY_QUEUE, PO_INPUT_QUEUE
from src.agents.po import tools, tools_projects
from src.allocations import AllocationError
from src.consumers.deploy import (
    _claim_deploy_job,
    _record_infrastructure_wait,
    _route_deploy_result,
)
from tests.service.test_public_deploy import (  # noqa: F401
    BUILT,
    CANARY,
    HEAD,
    public_project as existing_public_project,
    story_row,
)

public_project = existing_public_project


def scheduler(mode, story):
    result = subprocess.run(
        [sys.executable, "-P", str(Path(__file__).with_name("_initial_owner_scheduler.py"))],
        env=os.environ
        | {
            "PYTHONPATH": "/app/scheduler:/app",
            "API_BASE_URL": os.environ["TEST_API_BASE_URL"],
            "INITIAL_OWNER_MODE": mode,
            "INITIAL_OWNER_STORY": story,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert CANARY not in result.stdout + result.stderr


async def failed_source(api, stream, project, story, owner, route):
    await api.patch(
        f"stories/{story}",
        json={
            "generated_product_timeline": {
                "pull_request": {
                    "number": 42,
                    "state": "closed",
                    "head_sha": HEAD,
                    "merge_commit_sha": BUILT,
                    "merged_at": datetime.now(UTC).isoformat(),
                }
            }
        },
    )
    body = {
        "kind": "initial_owner",
        "story_id": story,
        "head_sha": HEAD,
        "deployed_commit_sha": BUILT,
    }
    admitted = await api.post(f"projects/{project}/users/grant-intents/lifecycle", json=body)
    source = admitted["execution_run_id"]
    if route != "poll":
        await api.transition_story(story, "deploy")
    message = DeployMessage(
        task_id=source,
        project_id=project,
        story_id=story,
        head_sha=HEAD,
        deployed_commit_sha=BUILT,
        telegram_chat_id=str(owner["telegram_id"]),
    )
    # The actual deploy result consumer writes the failed source Run.
    result = (
        {"missing_user_secrets": [{"key": "USER_SERVICE_KEY", "description": "User API key"}]}
        if route == "secret"
        else {
            "resolution_outcome": DeployOutcome.RETRY,
            "errors": ["controlled deployment failure"],
        }
    )
    source_before = await api.get(f"runs/{source}")
    await api.patch(
        f"runs/{source}",
        json={
            "run_metadata": source_before["run_metadata"]
            | {
                "test_diagnostic_canary": CANARY,
            }
        },
    )
    if route == "cancelled":
        lock = f"deploy:{project}:lock"
        assert await stream.redis.set(lock, "competing-deploy", nx=True, ex=60)
        try:
            with patch("src.consumers.deploy.api_client", api):
                terminal = await _claim_deploy_job(message, stream)
            assert terminal is not None
            assert terminal.response["status"] == "cancelled"
        finally:
            await stream.redis.delete(lock)
    elif route == "infrastructure":
        with patch("src.consumers.deploy.api_client", api):
            await _record_infrastructure_wait(
                source,
                project,
                AllocationError(
                    AllocationFailureReason.SERVER_NOT_PROVISIONED,
                    required_ram_mb=768,
                    min_disk_mb=1024,
                ),
            )
    else:
        with patch("src.consumers.deploy_failure_handler.api_client", api):
            await _route_deploy_result(result, SimpleNamespace(), message, stream)
    # A persisted diagnostic canary must not be copied into exhaustion,
    # either owed audience, logs or the PO readback/tool response.
    return source, message


@pytest.mark.asyncio
async def test_terminal_cancelled_owner_deploy_exhausts_and_can_retry(public_project, real_redis):
    async with asyncio.timeout(90):
        api, stream, project, story = public_project
        owner = await api.get(f"users/{(await api.get(f'projects/{project}'))['owner_id']}")
        await api.post(
            "system-configs/",
            json={"key": "deploy.max_deploy_retries", "value": 1, "category": "deploy"},
        )
        try:
            source, _ = await failed_source(api, stream, project, story, owner, "cancelled")
            run = await api.get(f"runs/{source}")
            assert run["status"] == "cancelled"
            assert run["result"]["deploy_outcome"] == "cancelled"
            assert (await story_row(story))["status"] == "deploying"
            scheduler("cancelled", story)
            stopped = await story_row(story)
            assert stopped["quarantine_reason"]["code"] == "initial_owner_deployment_exhausted"
            assert (
                "authenticated current initial-owner deployment readback"
                in stopped["quarantine_reason"]["detail"]
            )
            assert stopped["owner_notification"]["state"] == "owed"
            assert stopped["owner_notification"]["admin_state"] == "owed"
            assert "retry_initial_owner_deployment" not in stopped["owner_notification"]["text"]
            intent_id = run["run_metadata"]["users_grant_intent"]
            intent = await api.get_users_grant_intent(project, intent_id)
            assert intent.attempts == 1 and intent.execution_run_id == source
            assert intent.exhaustion.action == "retry_initial_owner_deployment"
            assert intent.exhaustion.retry_command.expected_execution_run_id == source
            with patch("src.agents.po.tools_projects._get_api", return_value=api):
                response = await tools_projects.retry_initial_owner_deployment.ainvoke(
                    {
                        "project_id": project,
                        "intent_id": intent_id,
                        "expected_execution_run_id": source,
                    },
                    config={"configurable": {"telegram_chat_id": str(owner["telegram_id"])}},
                )
            assert "dispatched" in response
            current = await api.get_users_grant_intent(project, intent_id)
            assert current.execution_run_id != source and len(current.retry_history) == 1
            assert (await story_row(story))["status"] == "deploying"
            assert (await api.get(f"runs/{source}")) == run
            attempts = [
                json.loads(data[b"data"])
                for _, data in await real_redis.xrange(DEPLOY_QUEUE)
                if json.loads(data[b"data"])["project_id"] == project
            ]
            assert [entry["task_id"] for entry in attempts] == [source, current.execution_run_id]
        finally:
            await api.post(
                "system-configs/",
                json={"key": "deploy.max_deploy_retries", "value": 3, "category": "deploy"},
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["retry", "cancelled"])
async def test_policy_zero_before_native_exhaustion_stops_without_a_command(
    public_project, real_redis, route
):
    async with asyncio.timeout(90):
        api, stream, project, story = public_project
        owner = await api.get(f"users/{(await api.get(f'projects/{project}'))['owner_id']}")
        await api.post(
            "system-configs/",
            json={"key": "deploy.max_deploy_retries", "value": 1, "category": "deploy"},
        )
        try:
            source, _ = await failed_source(api, stream, project, story, owner, route)
            run = await api.get(f"runs/{source}")
            assert run["status"] == ("cancelled" if route == "cancelled" else "failed")
            assert run["result"]["deploy_outcome"] == route
            await api.post(
                "system-configs/",
                json={"key": "deploy.max_deploy_retries", "value": 0, "category": "deploy"},
            )
            scheduler(route, story)
            stopped = await story_row(story)
            assert stopped["status"] == "failed"
            assert stopped["quarantine_reason"]["code"] == "initial_owner_deployment_exhausted"
            notice = stopped["owner_notification"]
            assert notice["state"] == notice["admin_state"] == "owed"
            assert "retry_initial_owner_deployment" not in json.dumps(stopped)
            intent_id = run["run_metadata"]["users_grant_intent"]
            intent = await api.get_users_grant_intent(project, intent_id)
            assert intent.attempts == 1 and intent.execution_run_id == source
            assert intent.exhaustion.exhausted_execution_run_id == source
            assert intent.exhaustion.action is None and intent.exhaustion.retry_command is None
            config = {"configurable": {"telegram_chat_id": str(owner["telegram_id"])}}
            with patch("src.agents.po.tools_projects._get_api", return_value=api):
                readback = json.loads(
                    await tools_projects.get_initial_owner_deployment.ainvoke(
                        {"project_id": project}, config=config
                    )
                )
                assert readback["exhaustion"]["action"] is None
                refusal = await tools_projects.retry_initial_owner_deployment.ainvoke(
                    {
                        "project_id": project,
                        "intent_id": intent_id,
                        "expected_execution_run_id": source,
                    },
                    config=config,
                )
                assert "exhausted" in refusal
            assert (await story_row(story)) == stopped
            assert (await api.get(f"runs/{source}")) == run
            scheduler("notice", story)
            delivered = await story_row(story)
            assert delivered["owner_notification"]["state"] == "delivered"
            assert delivered["owner_notification"]["admin_state"] == "delivered"
            await api.post(
                "system-configs/",
                json={"key": "deploy.max_deploy_retries", "value": 1, "category": "deploy"},
            )
            replay = await api.post(
                f"projects/{project}/users/grant-intents/lifecycle",
                json={
                    "kind": "initial_owner",
                    "story_id": story,
                    "head_sha": HEAD,
                    "deployed_commit_sha": BUILT,
                },
            )
            assert replay["disposition"] == "exhausted"
            assert replay["exhaustion"]["retry_command"] == {"expected_execution_run_id": source}
            available = await api.get_users_grant_intent(project, intent_id)
            assert available.exhaustion.retry_command.expected_execution_run_id == source
            assert (await story_row(story))["owner_notification"] == delivered["owner_notification"]
            assert len(available.retry_history) == 0
        finally:
            await api.post(
                "system-configs/",
                json={"key": "deploy.max_deploy_retries", "value": 3, "category": "deploy"},
            )


@pytest.mark.asyncio
async def test_zero_admission_poll_and_po_offer_no_retry(public_project, real_redis):
    async with asyncio.timeout(90):
        api, _, project, story = public_project
        owner = await api.get(f"users/{(await api.get(f'projects/{project}'))['owner_id']}")
        await api.post(
            "system-configs/",
            json={"key": "deploy.max_deploy_retries", "value": 0, "category": "deploy"},
        )
        try:
            await api.patch(
                f"stories/{story}",
                json={
                    "generated_product_timeline": {
                        "pull_request": {
                            "number": 42,
                            "state": "closed",
                            "head_sha": HEAD,
                            "merge_commit_sha": BUILT,
                            "merged_at": datetime.now(UTC).isoformat(),
                        }
                    }
                },
            )
            scheduler("poll", story)
            stopped = await story_row(story)
            assert stopped["status"] == "failed"
            assert stopped["quarantine_reason"]["code"] == "initial_owner_deployment_exhausted"
            assert (
                "same-target retry is unavailable" in stopped["quarantine_reason"]["detail"].lower()
            )
            notice = stopped["owner_notification"]
            assert notice["state"] == notice["admin_state"] == "owed"
            assert "same-target retry is unavailable" in notice["text"].lower()
            assert "retry_initial_owner_deployment" not in notice["text"] + notice["admin_text"]
            config = {"configurable": {"telegram_chat_id": str(owner["telegram_id"])}}
            with patch("src.agents.po.tools_projects._get_api", return_value=api):
                readback = await tools_projects.get_initial_owner_deployment.ainvoke(
                    {"project_id": project}, config=config
                )
                intent = json.loads(readback)
                assert intent["exhaustion"]["attempts"] == 0
                assert intent["exhaustion"]["exhausted_execution_run_id"] is None
                assert intent["exhaustion"]["action"] is None
                assert intent["exhaustion"]["retry_command"] is None
                refused = await tools_projects.retry_initial_owner_deployment.ainvoke(
                    {
                        "project_id": project,
                        "intent_id": intent["id"],
                        "expected_execution_run_id": "invented-run",
                    },
                    config=config,
                )
                assert "stale_target" in refused
            scheduler("poll", story)
            assert (await story_row(story))["owner_notification"] == notice
            await api.post(
                "system-configs/",
                json={"key": "deploy.max_deploy_retries", "value": 2, "category": "deploy"},
            )
            scheduler("poll", story)
            current = await api.get_users_grant_intent(project, intent["id"])
            assert current.attempts == 0 and current.exhaustion.action is None
            assert (await story_row(story))["owner_notification"] == notice
            scheduler("notice", story)
            delivered = await story_row(story)
            assert delivered["owner_notification"]["state"] == "delivered"
            assert delivered["owner_notification"]["admin_state"] == "delivered"
            events = [
                unprotect_po_payload(
                    PO_INPUT_QUEUE, {k.decode(): v.decode() for k, v in data.items()}
                )
                for _, data in await real_redis.xrange(PO_INPUT_QUEUE)
            ]
            event = next(e for e in events if e.get("story_id") == story)
            assert "same-target retry is unavailable" in event["text"].lower()
            assert "retry_initial_owner_deployment" not in event["text"]
            assert not any(
                json.loads(data[b"data"])["project_id"] == project
                for _, data in await real_redis.xrange(DEPLOY_QUEUE)
            )
        finally:
            await api.post(
                "system-configs/",
                json={"key": "deploy.max_deploy_retries", "value": 3, "category": "deploy"},
            )


@pytest.mark.asyncio
async def test_scheduler_discovers_committed_owner_retry_publication(public_project, real_redis):
    async with asyncio.timeout(90):
        api, stream, project, story = public_project
        owner = await api.get(f"users/{(await api.get(f'projects/{project}'))['owner_id']}")
        await api.post(
            "system-configs/",
            json={"key": "deploy.max_deploy_retries", "value": 1, "category": "deploy"},
        )
        try:
            source, _ = await failed_source(api, stream, project, story, owner, "retry")
            scheduler("retry", story)
            source_run = await api.get(f"runs/{source}")
            intent_id = source_run["run_metadata"]["users_grant_intent"]
            config = {"configurable": {"telegram_chat_id": str(owner["telegram_id"])}}
            saved_queue = f"{DEPLOY_QUEUE}:saved:{project}"
            await real_redis.rename(DEPLOY_QUEUE, saved_queue)
            await real_redis.set(DEPLOY_QUEUE, "controlled-publish-failure")
            try:
                with patch("src.agents.po.tools_projects._get_api", return_value=api):
                    answer = await tools_projects.retry_initial_owner_deployment.ainvoke(
                        {
                            "project_id": project,
                            "intent_id": intent_id,
                            "expected_execution_run_id": source,
                        },
                        config=config,
                    )
                assert "publication is owed" in answer
            finally:
                await real_redis.delete(DEPLOY_QUEUE)
                await real_redis.rename(saved_queue, DEPLOY_QUEUE)
            owed = await api.get_users_grant_intent(project, intent_id)
            assert owed.status.value == "publish_owed"
            assert owed.attempts == 1 and len(owed.retry_history) == 1
            fresh = owed.execution_run_id
            assert fresh != source
            assert (await story_row(story))["status"] == "deploying"
            attempts_before = [
                json.loads(data[b"data"])
                for _, data in await real_redis.xrange(DEPLOY_QUEUE)
                if json.loads(data[b"data"])["project_id"] == project
            ]
            assert [entry["task_id"] for entry in attempts_before] == [source]
            scheduler("owed", story)
            current = await api.get_users_grant_intent(project, intent_id)
            assert current.status.value == "queued"
            assert current.execution_run_id == fresh
            assert current.attempts == 1 and len(current.retry_history) == 1
            attempts_after = [
                json.loads(data[b"data"])
                for _, data in await real_redis.xrange(DEPLOY_QUEUE)
                if json.loads(data[b"data"])["project_id"] == project
            ]
            assert [entry["task_id"] for entry in attempts_after] == [source, fresh]
            assert attempts_after[-1]["head_sha"] == HEAD
            assert attempts_after[-1]["deployed_commit_sha"] == BUILT
            assert (await story_row(story))["status"] == "deploying"
        finally:
            await api.post(
                "system-configs/",
                json={"key": "deploy.max_deploy_retries", "value": 3, "category": "deploy"},
            )


async def assert_exhaustion_event(real_redis, story):
    events = [
        unprotect_po_payload(PO_INPUT_QUEUE, {k.decode(): v.decode() for k, v in data.items()})
        for _, data in await real_redis.xrange(PO_INPUT_QUEUE)
    ]
    event = next(
        e for e in events if e.get("story_id") == story and "exhausted" in e.get("text", "")
    )
    assert "retry_initial_owner_deployment" not in event["text"]
    assert "authenticated current initial-owner deployment readback" in event["text"]
    assert CANARY not in json.dumps(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["retry", "poll", "infrastructure", "secret"])
async def test_native_exhaustion_notice_and_po_retry(public_project, real_redis, route):
    # The service image has no pytest-timeout plugin. Keep the same bound with
    # the installed Python runtime rather than an unregistered marker.
    async with asyncio.timeout(180):
        await _native_exhaustion_notice_and_po_retry(public_project, real_redis, route)


async def _native_exhaustion_notice_and_po_retry(  # noqa: PLR0915
    public_project, real_redis, route
):
    api, stream, project, story = public_project
    owner = await api.get(f"users/{(await api.get(f'projects/{project}'))['owner_id']}")
    await api.post(
        "system-configs/",
        json={"key": "deploy.max_deploy_retries", "value": 1, "category": "deploy"},
    )
    try:
        source, message = await failed_source(api, stream, project, story, owner, route)
        scheduler(route, story)
        stopped = await story_row(story)
        assert stopped["status"] == "failed"
        assert stopped["quarantine_reason"]["code"] == "initial_owner_deployment_exhausted"
        assert (
            "authenticated current initial-owner deployment readback"
            in stopped["quarantine_reason"]["detail"]
        )
        notice = stopped["owner_notification"]
        assert notice["state"] == notice["admin_state"] == "owed"
        assert source in notice["text"] and HEAD in notice["text"]
        assert "retry_initial_owner_deployment" not in notice["text"] + notice["admin_text"]
        assert CANARY not in json.dumps(stopped)
        scheduler("notice_interrupt", story)
        interrupted = await story_row(story)
        assert interrupted["owner_notification"]["state"] == "owed"
        # Exercise the real claim interval in CI. No status, attempt or claim
        # timestamp is edited to get past the native delivery bound.
        await asyncio.sleep(61)
        scheduler("notice", story)
        settled = await story_row(story)
        assert settled["owner_notification"]["owed_at"] == notice["owed_at"]
        assert settled["owner_notification"]["state"] == "delivered"
        assert settled["owner_notification"]["admin_state"] == "delivered"
        await api.post(
            "system-configs/",
            json={"key": "deploy.max_deploy_retries", "value": 0, "category": "deploy"},
        )
        config = {"configurable": {"telegram_chat_id": str(owner["telegram_id"])}}
        with patch("src.agents.po.tools_projects._get_api", return_value=api):
            unavailable = json.loads(
                await tools_projects.get_initial_owner_deployment.ainvoke(
                    {"project_id": project}, config=config
                )
            )
            assert unavailable["exhaustion"]["action"] is None
            assert unavailable["exhaustion"]["retry_command"] is None
            refused = await tools_projects.retry_initial_owner_deployment.ainvoke(
                {
                    "project_id": project,
                    "intent_id": unavailable["id"],
                    "expected_execution_run_id": source,
                },
                config=config,
            )
            assert "exhausted" in refused
        assert (await story_row(story))["owner_notification"] == settled["owner_notification"]
        await api.post(
            "system-configs/",
            json={"key": "deploy.max_deploy_retries", "value": 1, "category": "deploy"},
        )
        with patch("src.agents.po.tools_projects._get_api", return_value=api):
            readback = await tools_projects.get_initial_owner_deployment.ainvoke(
                {"project_id": project}, config=config
            )
            intent = json.loads(readback)
            assert CANARY not in readback
            assert intent["exhaustion"]["code"] == "initial_owner_deployment_exhausted"
            assert intent["exhaustion"]["action"] == "retry_initial_owner_deployment"
            assert intent["exhaustion"]["retry_command"] == {"expected_execution_run_id": source}
            assert tools_projects.retry_initial_owner_deployment in tools.get_all_tools()
            with capture_logs() as logs:
                outputs = await asyncio.gather(
                    *[
                        tools_projects.retry_initial_owner_deployment.ainvoke(
                            {
                                "project_id": project,
                                "intent_id": intent["id"],
                                "expected_execution_run_id": source,
                            },
                            config=config,
                        )
                        for _ in range(3)
                    ]
                )
            assert sum("dispatched" in text for text in outputs) == 1
            assert CANARY not in json.dumps(outputs) + json.dumps(logs)
        recovered = await api.get_story(story)
        assert recovered.status.value == "deploying" and recovered.quarantine_reason is None
        current = await api.get_users_grant_intent(project, intent["id"])
        assert current.attempts == 1 and len(current.retry_history) == 1
        fresh = current.execution_run_id
        rows = await real_redis.xrange(DEPLOY_QUEUE)
        attempts = [
            json.loads(data[b"data"])
            for _, data in rows
            if json.loads(data[b"data"])["project_id"] == project
        ]
        assert len(attempts) == 2
        queued = next(data for data in attempts if data["task_id"] == fresh)
        assert queued["story_id"] == story
        assert queued["head_sha"] == HEAD and queued["deployed_commit_sha"] == BUILT
        # The next epoch reaches the actual consumer and ordinary supervisor,
        # retaining the recovered Story state without a manual resume.
        with patch("src.consumers.deploy_failure_handler.api_client", api):
            await _route_deploy_result(
                {
                    "resolution_outcome": DeployOutcome.RETRY,
                    "errors": ["controlled second epoch failure"],
                },
                SimpleNamespace(),
                message.model_copy(update={"task_id": fresh}),
                stream,
            )
        scheduler("retry", story)
        final = await api.get_users_grant_intent(project, intent["id"])
        assert final.attempts == 1 and len(final.retry_history) == 1
        assert final.status.value == "failed"
        # Owed delivery for the latest genuine exhausted epoch is attempted
        # immediately and recovers both audiences through normal supervision.
        scheduler("notice", story)
        delivered = await story_row(story)
        assert delivered["owner_notification"]["state"] == "delivered"
        assert delivered["owner_notification"]["admin_state"] == "delivered"
        count = await real_redis.xlen(PO_INPUT_QUEUE)
        scheduler("notice", story)
        assert await real_redis.xlen(PO_INPUT_QUEUE) == count
        await assert_exhaustion_event(real_redis, story)
        # An old request cannot reset the fast-completed new exhausted epoch.
        with patch("src.agents.po.tools_projects._get_api", return_value=api):
            replay = await tools_projects.retry_initial_owner_deployment.ainvoke(
                {
                    "project_id": project,
                    "intent_id": intent["id"],
                    "expected_execution_run_id": source,
                },
                config=config,
            )
        assert "stale_target" in replay
    finally:
        await api.post(
            "system-configs/",
            json={"key": "deploy.max_deploy_retries", "value": 3, "category": "deploy"},
        )
