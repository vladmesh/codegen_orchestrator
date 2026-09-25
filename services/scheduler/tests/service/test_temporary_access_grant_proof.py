"""Real API proof that temporary QA access counts as granted only when the product confirmed it.

On 2026-09-15 every capability deploy run completed `SUCCESS` with
`skipped_reason=already_deployed_same_sha`: the product never saw the grant,
QA was released anyway and the QA identity was answered "no access". Here the
sweep runs against the real API with the grant run in exactly that state.
"""

import json
import os
import uuid

import httpx
import pytest

from shared.contracts.dto.run_result import (
    DeployRunResult,
    DeploySkipReason,
    QABlockerCategory,
    QARunResult,
)
from shared.contracts.dto.temporary_access import TemporaryAccessRevokeReason, TemporaryAccessStatus
from shared.contracts.queues.deploy import DeployAction, DeployOutcome
from shared.contracts.queues.qa import QAMessage, QAOutcome
from shared.queues import QA_QUEUE
from shared.redis import RedisStreamClient
from shared.tests.ssh_key_fixtures import fleet_private_key
from src.tasks.temporary_access import (
    _max_grant_attempts,
    grant_temporary_access,
    supervise_temporary_access,
)

HEAD_SHA = "c" * 40
BUILT_SHA = "d" * 40
TARGET_URL = "https://grant-proof.example.com"


class _API:
    """The real API through the scheduler's own internal client, statuses returned as-is."""

    def __init__(self, client) -> None:
        self._client = client

    async def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        return await self._client.request_raw(method, path.removeprefix("/api/"), **kwargs)

    async def get(self, path: str, **kwargs) -> httpx.Response:
        return await self._send("GET", path, **kwargs)

    async def post(self, path: str, **kwargs) -> httpx.Response:
        return await self._send("POST", path, **kwargs)

    async def patch(self, path: str, **kwargs) -> httpx.Response:
        return await self._send("PATCH", path, **kwargs)


@pytest.fixture
def async_client(api_client) -> _API:
    return _API(api_client)


@pytest.fixture
async def redis_client():
    client = RedisStreamClient(os.environ["REDIS_URL"])
    await client.connect()
    try:
        yield client
    finally:
        await client.close()


async def _target_with_qa_run(client: _API) -> tuple[str, int, str]:
    """A running application with a successful deployment record and a queued QA run."""
    suffix = uuid.uuid4().hex[:8]
    telegram_id = uuid.uuid4().int % 1_000_000_000
    user = await client.post(
        "/api/users/", json={"telegram_id": telegram_id, "username": f"proof-{telegram_id}"}
    )
    assert user.is_success, user.text
    project_id = str(uuid.uuid4())
    project = await client.post(
        "/api/projects/",
        json={
            "id": project_id,
            "title": "Grant proof",
            "initiating_run_id": f"init-{suffix}",
            "status": "active",
            "config": {"workspace_ready": True},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert project.is_success, project.text
    server = await client.post(
        "/api/servers/",
        json={
            "handle": f"proof-{suffix}",
            "host": "h.test",
            "public_ip": "10.9.0.10",
            "ssh_key": fleet_private_key(),
        },
    )
    assert server.status_code == httpx.codes.CREATED, server.text
    repository = await client.post(
        "/api/repositories/",
        json={
            "project_id": project_id,
            "name": f"proof-{suffix}",
            "git_url": f"https://github.com/test/proof-{suffix}.git",
        },
    )
    assert repository.status_code == httpx.codes.CREATED, repository.text
    application = await client.post(
        "/api/applications/",
        json={
            "repo_id": repository.json()["id"],
            "server_handle": server.json()["handle"],
            "service_name": f"proof-{suffix}",
            "status": "running",
        },
    )
    assert application.status_code == httpx.codes.CREATED, application.text
    application_id = application.json()["id"]
    deployment = await client.post(
        "/api/service-deployments/",
        json={
            "application_id": application_id,
            "project_id": project_id,
            "service_name": f"proof-{suffix}",
            "server_handle": server.json()["handle"],
            "port": 8080,
            "result": "success",
            "deployed_sha": HEAD_SHA,
            "deployment_info": {"deployed_commit_sha": BUILT_SHA},
        },
    )
    assert deployment.status_code == httpx.codes.CREATED, deployment.text
    qa = await client.post(
        "/api/work-admission/paid-runs",
        json={
            "id": f"qa-proof-{suffix}",
            "type": "qa",
            "project_id": project_id,
            "run_metadata": {"application_id": application_id},
        },
    )
    assert qa.status_code == httpx.codes.OK, qa.text
    return project_id, application_id, qa.json()["run_id"]


async def _grant(client: _API, qa_run_id: str) -> dict:
    rows = (
        await client.get("/api/temporary-access-grants/", params={"qa_run_id": qa_run_id})
    ).json()
    assert len(rows) == 1, rows
    return rows[0]


async def _complete_operation(
    client: _API, run_id: str, application_id: int, skipped_reason: DeploySkipReason | None
) -> None:
    settled = await client.patch(
        f"/api/runs/{run_id}",
        json={
            "status": "completed",
            "result": DeployRunResult(
                deploy_outcome=DeployOutcome.SUCCESS,
                deployed_url=TARGET_URL,
                application_id=application_id,
                skipped_reason=skipped_reason,
            ).model_dump(mode="json"),
        },
    )
    assert settled.status_code == httpx.codes.OK, settled.text


async def _qa_messages_for(redis_client: RedisStreamClient, qa_run_id: str) -> int:
    entries = await redis_client.redis.xrange(QA_QUEUE)
    return sum(1 for _, fields in entries if json.loads(fields["data"])["run_id"] == qa_run_id)


async def _start_grant(
    api_client, redis_client, project_id: str, application_id: int, qa_run_id: str
) -> None:
    grant = await grant_temporary_access(
        api_client,
        redis_client,
        project_id=project_id,
        target_application_id=application_id,
        target_base_url=TARGET_URL,
        head_sha=HEAD_SHA,
        # A run-owned QA message: the sweep never reads a story.
        qa_message=QAMessage(
            project_id=project_id,
            initiating_run_id=f"deploy-{qa_run_id}",
            telegram_chat_id="",
            deployed_url=TARGET_URL,
            application_id=application_id,
            acceptance_criteria="the bot answers /start",
            run_id=qa_run_id,
        ),
    )
    assert grant is not None
    assert grant.status is TemporaryAccessStatus.GRANTING


@pytest.mark.asyncio
async def test_skipped_grant_runs_never_release_qa_and_end_in_the_access_blocker(
    async_client, api_client, redis_client
):
    project_id, application_id, qa_run_id = await _target_with_qa_run(async_client)
    await _start_grant(api_client, redis_client, project_id, application_id, qa_run_id)

    for _ in range(_max_grant_attempts()):
        grant = await _grant(async_client, qa_run_id)
        assert grant["status"] == TemporaryAccessStatus.GRANTING.value
        # The deploy consumer's shortcut: the run completes without reaching the product.
        await _complete_operation(
            async_client,
            grant["grant_run_id"],
            application_id,
            DeploySkipReason.ALREADY_DEPLOYED_SAME_SHA,
        )
        await supervise_temporary_access(api_client, redis_client)
        assert await _qa_messages_for(redis_client, qa_run_id) == 0

    grant = await _grant(async_client, qa_run_id)
    assert grant["status"] == TemporaryAccessStatus.REVOKING.value
    assert grant["revoke_reason"] == TemporaryAccessRevokeReason.GRANT_FAILED.value
    assert grant["qa_dispatched_at"] is None
    qa_run = (await async_client.get(f"/api/runs/{qa_run_id}")).json()
    assert qa_run["status"] == "failed"
    assert qa_run["result"]["blocker"]["category"] == QABlockerCategory.QA_ACCESS_GRANT_FAILED.value
    assert await _qa_messages_for(redis_client, qa_run_id) == 0


@pytest.mark.asyncio
async def test_confirmed_grant_releases_qa_exactly_once(async_client, api_client, redis_client):
    project_id, application_id, qa_run_id = await _target_with_qa_run(async_client)
    await _start_grant(api_client, redis_client, project_id, application_id, qa_run_id)
    grant = await _grant(async_client, qa_run_id)
    # Only an unskipped run that read the access back active records SUCCESS.
    await _complete_operation(async_client, grant["grant_run_id"], application_id, None)

    await supervise_temporary_access(api_client, redis_client)
    await supervise_temporary_access(api_client, redis_client)

    grant = await _grant(async_client, qa_run_id)
    assert grant["status"] == TemporaryAccessStatus.GRANTED.value
    assert grant["qa_dispatched_at"] is not None
    assert await _qa_messages_for(redis_client, qa_run_id) == 1


async def _qa_run_on(client: _API, project_id: str, application_id: int) -> str:
    """A second paid QA run on the same project and the same deployed application."""
    admitted = await client.post(
        "/api/work-admission/paid-runs",
        json={
            "id": f"qa-second-{uuid.uuid4().hex[:8]}",
            "type": "qa",
            "project_id": project_id,
            "run_metadata": {"application_id": application_id},
        },
    )
    assert admitted.status_code == httpx.codes.OK, admitted.text
    # A denied admission would be a shared paid-run ceiling, not this contract.
    assert admitted.json()["admission"]["outcome"] == "admitted", admitted.text
    return admitted.json()["run_id"]


async def _finish_qa_run(client: _API, qa_run_id: str) -> None:
    """The QA verdict that ends the first story and releases its access."""
    settled = await client.patch(
        f"/api/runs/{qa_run_id}",
        json={
            "status": "completed",
            "result": QARunResult(qa_outcome=QAOutcome.PASSED, summary="health only").model_dump(
                mode="json"
            ),
        },
    )
    assert settled.status_code == httpx.codes.OK, settled.text


async def _settle_grant_through_to_granted(
    async_client, api_client, redis_client, project_id: str, application_id: int, qa_run_id: str
) -> None:
    await _start_grant(api_client, redis_client, project_id, application_id, qa_run_id)
    grant = await _grant(async_client, qa_run_id)
    await _complete_operation(async_client, grant["grant_run_id"], application_id, None)
    await supervise_temporary_access(api_client, redis_client)
    assert (await _grant(async_client, qa_run_id))["status"] == TemporaryAccessStatus.GRANTED.value


@pytest.mark.asyncio
async def test_the_second_story_gets_access_once_the_first_story_released_its_grant(
    async_client, api_client, redis_client
):
    """The sequence stand-e2e run 35470184817 could not get through.

    The first story's QA had long since passed, and its grant still held
    application 1 when the second story's handoff asked for the same target, so
    that handoff was refused and the story sat in TESTING until the harness gave
    up. Here the first grant walks its whole lifecycle on the real API — granted,
    QA terminal, cleanup proved, `REVOKED` — and the second story's handoff then
    has to get through.
    """
    project_id, application_id, first_qa_run_id = await _target_with_qa_run(async_client)
    await _settle_grant_through_to_granted(
        async_client, api_client, redis_client, project_id, application_id, first_qa_run_id
    )

    # The first story is over: its QA reached a verdict.
    await _finish_qa_run(async_client, first_qa_run_id)
    await supervise_temporary_access(api_client, redis_client)
    first = await _grant(async_client, first_qa_run_id)
    assert first["status"] == TemporaryAccessStatus.REVOKING.value
    assert first["revoke_reason"] == TemporaryAccessRevokeReason.RUN_TERMINAL.value

    # The capability revoke proves the access inactive, and nothing else about
    # that redeploy may turn the proof into a failure.
    await _complete_operation(async_client, first["revoke_run_id"], application_id, None)
    await supervise_temporary_access(api_client, redis_client)
    assert (await _grant(async_client, first_qa_run_id))[
        "status"
    ] == TemporaryAccessStatus.REVOKED.value

    second_qa_run_id = await _qa_run_on(async_client, project_id, application_id)
    await _start_grant(api_client, redis_client, project_id, application_id, second_qa_run_id)

    second = await _grant(async_client, second_qa_run_id)
    assert second["status"] == TemporaryAccessStatus.GRANTING.value
    assert second["target_application_id"] == application_id
    assert second["id"] != first["id"]

    # Leave no queued paid run behind: the ceiling this suite shares is global.
    await _finish_qa_run(async_client, second_qa_run_id)


@pytest.mark.asyncio
async def test_a_revoke_that_lands_after_undeploy_closes_the_grant(
    async_client, api_client, redis_client
):
    """Stand run 36075631307: the suite undeployed before the revoke ran.

    The deploy consumer now allocates nothing for that revoke and records what a
    proved revoke records; the real API has to accept it as revoke proof, and
    the grant closes with no retry and no escalation.
    """
    project_id, application_id, qa_run_id = await _target_with_qa_run(async_client)
    await _settle_grant_through_to_granted(
        async_client, api_client, redis_client, project_id, application_id, qa_run_id
    )
    await _finish_qa_run(async_client, qa_run_id)
    undeployed = await async_client.patch(
        f"/api/applications/{application_id}", json={"status": "not_deployed"}
    )
    assert undeployed.status_code == httpx.codes.OK, undeployed.text
    await supervise_temporary_access(api_client, redis_client)
    revoking = await _grant(async_client, qa_run_id)
    assert revoking["status"] == TemporaryAccessStatus.REVOKING.value

    # The deploy consumer's record for a revoke whose target has no allocations.
    settled = await async_client.patch(
        f"/api/runs/{revoking['revoke_run_id']}",
        json={
            "status": "completed",
            "result": DeployRunResult(
                deploy_outcome=DeployOutcome.SUCCESS, action=DeployAction.FEATURE
            ).model_dump(mode="json"),
        },
    )
    assert settled.status_code == httpx.codes.OK, settled.text
    await supervise_temporary_access(api_client, redis_client)

    grant = await _grant(async_client, qa_run_id)
    assert grant["status"] == TemporaryAccessStatus.REVOKED.value
    assert grant["revoke_run_id"] == revoking["revoke_run_id"]
    assert grant["revoke_attempts"] == revoking["revoke_attempts"]
    assert grant["escalated_at"] is None


@pytest.mark.asyncio
async def test_a_live_first_grant_still_refuses_the_second_story(
    async_client, api_client, redis_client
):
    """The 409 guard stays: two live grants on one application are still refused."""
    project_id, application_id, first_qa_run_id = await _target_with_qa_run(async_client)
    await _settle_grant_through_to_granted(
        async_client, api_client, redis_client, project_id, application_id, first_qa_run_id
    )

    second_qa_run_id = await _qa_run_on(async_client, project_id, application_id)
    refused = await grant_temporary_access(
        api_client,
        redis_client,
        project_id=project_id,
        target_application_id=application_id,
        target_base_url=TARGET_URL,
        head_sha=HEAD_SHA,
        qa_message=QAMessage(
            project_id=project_id,
            initiating_run_id=f"deploy-{second_qa_run_id}",
            telegram_chat_id="",
            deployed_url=TARGET_URL,
            application_id=application_id,
            acceptance_criteria="the bot answers /start",
            run_id=second_qa_run_id,
        ),
    )

    assert refused is None
    # No second record exists, and the first is untouched and still live.
    assert (
        await async_client.get(
            "/api/temporary-access-grants/", params={"qa_run_id": second_qa_run_id}
        )
    ).json() == []
    assert (await _grant(async_client, first_qa_run_id))[
        "status"
    ] == TemporaryAccessStatus.GRANTED.value
    # Inside its bound the refusal settles nothing: the QA run stays queued.
    second_run = (await async_client.get(f"/api/runs/{second_qa_run_id}")).json()
    assert second_run["status"] == "queued"

    # Leave no queued paid run behind: the ceiling this suite shares is global.
    await _finish_qa_run(async_client, second_qa_run_id)
    await _finish_qa_run(async_client, first_qa_run_id)
