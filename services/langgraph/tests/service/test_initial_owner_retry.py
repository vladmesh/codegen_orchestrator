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
from src.consumers.deploy import _record_infrastructure_wait, _route_deploy_result
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
    if route == "infrastructure":
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


async def assert_exhaustion_event(real_redis, story):
    events = [
        unprotect_po_payload(PO_INPUT_QUEUE, {k.decode(): v.decode() for k, v in data.items()})
        for _, data in await real_redis.xrange(PO_INPUT_QUEUE)
    ]
    event = next(
        e for e in events if e.get("story_id") == story and "exhausted" in e.get("text", "")
    )
    assert "retry_initial_owner_deployment" in event["text"] and CANARY not in json.dumps(event)


@pytest.mark.asyncio
@pytest.mark.timeout(180)
@pytest.mark.parametrize("route", ["retry", "poll", "infrastructure", "secret"])
async def test_native_exhaustion_notice_and_po_retry(public_project, real_redis, route):
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
        notice = stopped["owner_notification"]
        assert notice["state"] == notice["admin_state"] == "owed"
        assert source in notice["text"] and HEAD in notice["text"]
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
        with patch("src.agents.po.tools_projects._get_api", return_value=api):
            config = {"configurable": {"telegram_chat_id": str(owner["telegram_id"])}}
            readback = await tools_projects.get_initial_owner_deployment.ainvoke(
                {"project_id": project}, config=config
            )
            intent = json.loads(readback)
            assert CANARY not in readback
            assert intent["exhaustion"]["code"] == "initial_owner_deployment_exhausted"
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
