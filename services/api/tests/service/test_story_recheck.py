"""Service coverage for the audited operator QA recheck action."""

from http import HTTPStatus
import json
import uuid

from httpx import AsyncClient
import pytest
from redis.asyncio import Redis

from shared.contracts.dto.qa_handoff import QA_HANDOFF_KEY, QAHandoffPlan
from shared.contracts.queues.qa import QAMessage
from shared.redis.client import decode_redis_fields

HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"


async def _story_quarantined_by(  # noqa: PLR0913, PLR0915
    async_client: AsyncClient,
    *,
    category: str,
    attempted: str,
    sent: str,
    received: str,
) -> dict:
    """One story parked in `waiting_human_review` by one typed QA blocker.

    The whole chain is real — user, project, server, repository, application,
    port allocation, deploy receipt, admitted QA run — because the recheck
    reads the QA Run as its capability receipt and refuses an implied target.
    The blocker's category is the only thing that varies between the callers,
    which is what makes this the right seam: the tests below differ in which
    failure an operator repaired, and in nothing else.
    """
    head_sha = HEAD_SHA

    telegram_id = uuid.uuid4().int % 1_000_000_000
    project_id = str(uuid.uuid4())
    created_user = await async_client.post(
        "/api/users/", json={"telegram_id": telegram_id, "username": f"recheck_{telegram_id}"}
    )
    assert created_user.status_code == HTTPStatus.CREATED
    project = await async_client.post(
        "/api/projects/",
        json={
            "id": project_id,
            "initiating_run_id": "recheck-init-run",
            "title": "Recheck QA",
            "config": {},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert project.status_code == HTTPStatus.CREATED, project.text
    server = await async_client.post(
        "/api/servers/",
        json={
            "handle": f"recheck-{uuid.uuid4().hex[:8]}",
            "host": "recheck.example.test",
            "public_ip": "10.0.0.9",
            "ssh_user": "root",
        },
    )
    assert server.status_code == HTTPStatus.CREATED, server.text
    server_handle = server.json()["handle"]
    repository = await async_client.post(
        "/api/repositories/",
        json={
            "project_id": project_id,
            "name": "recheck-repository",
            "git_url": "https://github.com/test/recheck-repository.git",
        },
    )
    assert repository.status_code == HTTPStatus.CREATED, repository.text
    application = await async_client.post(
        "/api/applications/",
        json={
            "repo_id": repository.json()["id"],
            "server_handle": server_handle,
            "service_name": "recheck-service",
            "status": "stopped",
        },
    )
    assert application.status_code == HTTPStatus.CREATED, application.text
    application_id = application.json()["id"]
    allocated = await async_client.post(
        f"/api/servers/{server_handle}/ports/allocate-next",
        json={"service_name": "recheck-service", "application_id": application_id},
    )
    assert allocated.status_code == HTTPStatus.OK, allocated.text

    story = await async_client.post(
        "/api/stories/", json={"project_id": project_id, "title": "Recover QA"}
    )
    assert story.status_code == HTTPStatus.CREATED, story.text
    story_id = story.json()["id"]
    assert (await async_client.post(f"/api/stories/{story_id}/start")).status_code == HTTPStatus.OK
    assert (
        await async_client.post(f"/api/stories/{story_id}/human-review")
    ).status_code == HTTPStatus.OK
    deploy_receipt = await async_client.post(
        "/api/runs/",
        json={
            "id": f"deploy-receipt-{uuid.uuid4().hex[:12]}",
            "type": "deploy",
            "project_id": project_id,
            "story_id": story_id,
            "run_metadata": {"application_id": application_id, "head_sha": head_sha},
        },
    )
    assert deploy_receipt.status_code == HTTPStatus.CREATED, deploy_receipt.text
    quarantined_qa_id = f"qa-quarantine-{uuid.uuid4().hex[:12]}"
    qa_run = await async_client.post(
        "/api/work-admission/paid-runs",
        json={
            "id": quarantined_qa_id,
            "type": "qa",
            "project_id": project_id,
            "story_id": story_id,
            "run_metadata": {
                "application_id": application_id,
                QA_HANDOFF_KEY: QAHandoffPlan(
                    qa_message=QAMessage(
                        project_id=project_id,
                        initiating_run_id="recheck-init-run",
                        deployed_url="http://10.0.0.9:8000",
                        application_id=application_id,
                        acceptance_criteria="health endpoint answers",
                        run_id=quarantined_qa_id,
                    )
                ).model_dump(mode="json"),
            },
        },
    )
    assert qa_run.status_code == HTTPStatus.OK, qa_run.text
    qa_run_id = qa_run.json()["run_id"]
    qa_result = {
        "qa_outcome": "blocked",
        "blocker": {
            "category": category,
            "attempted": attempted,
            "sent": sent,
            "received": received,
        },
    }
    completed_qa = await async_client.patch(
        f"/api/runs/{qa_run_id}",
        json={"status": "failed", "result": qa_result},
    )
    assert completed_qa.status_code == HTTPStatus.OK, completed_qa.text
    quarantined = await async_client.patch(
        f"/api/stories/{story_id}",
        json={"quarantine_reason": qa_result},
    )
    assert quarantined.status_code == HTTPStatus.OK, quarantined.text
    return {
        "story_id": story_id,
        "application_id": application_id,
        "deploy_receipt_id": deploy_receipt.json()["id"],
        "qa_run_id": qa_run_id,
    }


@pytest.mark.asyncio
async def test_recheck_stopped_qa_quarantine_creates_one_story_linked_deploy(  # noqa: PLR0915
    async_client: AsyncClient, redis_client: Redis
) -> None:
    """A repaired infrastructure target returns through deploy and ordinary QA."""
    head_sha = HEAD_SHA
    parked = await _story_quarantined_by(
        async_client,
        category="server_unavailable",
        attempted="connect QA probe",
        sent="GET /health",
        received="connection refused",
    )
    story_id = parked["story_id"]
    application_id = parked["application_id"]
    deploy_receipt_id = parked["deploy_receipt_id"]

    headers = {"X-Admin-Console-Operator": "shared-console"}
    accepted_while_stopped = await async_client.post(
        f"/api/stories/{story_id}/accept-result",
        json={"basis": "Manual review."},
        headers=headers,
    )
    assert accepted_while_stopped.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "application to be running" in accepted_while_stopped.text

    sideways_e2e = await async_client.post(f"/api/applications/{application_id}/run-e2e")
    assert sideways_e2e.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "quarantined story" in sideways_e2e.text

    before = await redis_client.xlen("deploy:queue")
    first = await async_client.post(
        f"/api/stories/{story_id}/recheck-qa",
        json={"basis": "The server was repaired and is reachable."},
        headers=headers,
    )
    assert first.status_code == HTTPStatus.OK, first.text
    body = first.json()
    assert body["status"] == "deploying"
    assert body["quarantine_reason"] is not None
    audit = body["operator_recheck"]
    assert audit["actor"] == "admin_console:shared-console"
    assert audit["basis"] == "The server was repaired and is reachable."
    assert audit["mode"] == "deploy"

    message_rows = await redis_client.xrevrange("deploy:queue", count=1)
    assert await redis_client.xlen("deploy:queue") == before + 1
    message = json.loads(decode_redis_fields(message_rows[0][1])["data"])
    assert message["story_id"] == story_id
    assert message["application_id"] == application_id
    assert message["head_sha"] == head_sha
    target = await async_client.get(f"/api/applications/{application_id}")
    assert target.json()["status"] == "deploying"
    run = await async_client.get(f"/api/runs/{message['task_id']}")
    assert run.status_code == HTTPStatus.OK
    assert run.json()["story_id"] == story_id
    assert run.json()["run_metadata"]["application_id"] == application_id
    assert run.json()["run_metadata"]["recheck_id"] == audit["id"]
    assert run.json()["run_metadata"]["source_deploy_run_id"] == deploy_receipt_id
    assert run.json()["run_metadata"]["head_sha"] == head_sha

    repeated = await async_client.post(
        f"/api/stories/{story_id}/recheck-qa",
        json={"basis": "The server was repaired and is reachable."},
        headers=headers,
    )
    assert repeated.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, repeated.text
    assert "waiting_human_review" in repeated.text
    assert await redis_client.xlen("deploy:queue") == before + 1

    # A completed recheck run and a new typed quarantine start a new recovery
    # episode. The old audit must not permanently consume this story's route
    # back to ordinary QA.
    terminal = await async_client.patch(
        f"/api/runs/{audit['run_id']}",
        json={
            "status": "failed",
            "result": {"deploy_outcome": "give_up", "error_details": "environment failed again"},
        },
    )
    assert terminal.status_code == HTTPStatus.OK, terminal.text
    parked_again = await async_client.post(f"/api/stories/{story_id}/human-review")
    assert parked_again.status_code == HTTPStatus.OK, parked_again.text
    fresh_blocker = {
        "qa_outcome": "blocked",
        "blocker": {
            "category": "qa_executor_unavailable",
            "attempted": "start QA executor",
            "sent": "qa execution request",
            "received": "executor unavailable",
        },
    }
    refreshed_reason = await async_client.patch(
        f"/api/stories/{story_id}", json={"quarantine_reason": fresh_blocker}
    )
    assert refreshed_reason.status_code == HTTPStatus.OK, refreshed_reason.text
    stopped_again = await async_client.patch(
        f"/api/applications/{application_id}", json={"status": "stopped"}
    )
    assert stopped_again.status_code == HTTPStatus.OK, stopped_again.text

    rechecked_again = await async_client.post(
        f"/api/stories/{story_id}/recheck-qa",
        json={"basis": "The QA executor was repaired."},
        headers=headers,
    )
    assert rechecked_again.status_code == HTTPStatus.OK, rechecked_again.text
    assert rechecked_again.json()["operator_recheck"]["id"] != audit["id"]
    assert rechecked_again.json()["operator_recheck"]["run_id"] != audit["run_id"]
    assert await redis_client.xlen("deploy:queue") == before + 2


@pytest.mark.asyncio
async def test_a_repaired_administrative_account_can_be_rechecked(
    async_client: AsyncClient, redis_client: Redis
) -> None:
    """Naming the permission problem must not close the operator's route back.

    Before `qa_identity_unreadable` existed, this exact failure — the account
    the fleet key opens could not read the QA account's `authorized_keys` —
    arrived as `server_unavailable` and could be re-checked once an operator
    put the administrative account back on the server row. It is repaired
    outside the code and then wants the same recheck, so it is in the same set.
    """
    parked = await _story_quarantined_by(
        async_client,
        category="qa_identity_unreadable",
        attempted="issue a one-shot QA identity on the target",
        sent="authorized_keys entry codegen-qa-run-abc on 10.0.0.9",
        received="cannot search /home/qa-observer as deploy",
    )
    before = await redis_client.xlen("deploy:queue")

    rechecked = await async_client.post(
        f"/api/stories/{parked['story_id']}/recheck-qa",
        json={"basis": "The server row names the administrative account again."},
        headers={"X-Admin-Console-Operator": "shared-console"},
    )

    assert rechecked.status_code == HTTPStatus.OK, rechecked.text
    assert rechecked.json()["status"] == "deploying"
    assert await redis_client.xlen("deploy:queue") == before + 1
    message = json.loads(
        decode_redis_fields((await redis_client.xrevrange("deploy:queue", count=1))[0][1])["data"]
    )
    assert message["story_id"] == parked["story_id"]
    assert message["application_id"] == parked["application_id"]
