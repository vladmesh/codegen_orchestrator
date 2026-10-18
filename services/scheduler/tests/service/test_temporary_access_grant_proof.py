"""Real API proof that temporary QA access counts as granted only when the product confirmed it.

On 2026-09-15 every capability deploy run completed `SUCCESS` with
`skipped_reason=already_deployed_same_sha`: the product never saw the grant,
QA was released anyway and the QA identity was answered "no access". Here the
sweep runs against the real API with the grant run in exactly that state.
"""

import os
import uuid

from _qa_queue import qa_messages_for
from _temporary_access_grants import (
    HEAD_SHA,
    TARGET_URL,
    RawAPI,
    complete_operation,
    finish_qa_run,
    grant_for,
    start_grant,
    target_with_qa_run,
)
import httpx
import pytest

from shared.contracts.dto.run_result import DeployRunResult, DeploySkipReason, QABlockerCategory
from shared.contracts.dto.temporary_access import TemporaryAccessRevokeReason, TemporaryAccessStatus
from shared.contracts.queues.deploy import DeployAction, DeployOutcome
from shared.contracts.queues.qa import QAMessage
from shared.redis import RedisStreamClient
from src.tasks.temporary_access import (
    _max_grant_attempts,
    grant_temporary_access,
    supervise_temporary_access,
)


@pytest.fixture
def async_client(api_client) -> RawAPI:
    return RawAPI(api_client)


@pytest.fixture
async def redis_client():
    client = RedisStreamClient(os.environ["REDIS_URL"])
    await client.connect()
    try:
        yield client
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_skipped_grant_runs_never_release_qa_and_end_in_the_access_blocker(
    async_client, api_client, redis_client
):
    project_id, application_id, qa_run_id = await target_with_qa_run(async_client)
    await start_grant(api_client, redis_client, project_id, application_id, qa_run_id)

    for _ in range(_max_grant_attempts()):
        grant = await grant_for(async_client, qa_run_id)
        assert grant["status"] == TemporaryAccessStatus.GRANTING.value
        # The deploy consumer's shortcut: the run completes without reaching the product.
        await complete_operation(
            async_client,
            grant["grant_run_id"],
            application_id,
            DeploySkipReason.ALREADY_DEPLOYED_SAME_SHA,
        )
        await supervise_temporary_access(api_client, redis_client)
        assert await qa_messages_for(redis_client, qa_run_id) == 0

    grant = await grant_for(async_client, qa_run_id)
    assert grant["status"] == TemporaryAccessStatus.REVOKING.value
    assert grant["revoke_reason"] == TemporaryAccessRevokeReason.GRANT_FAILED.value
    assert grant["qa_dispatched_at"] is None
    qa_run = (await async_client.get(f"/api/runs/{qa_run_id}")).json()
    assert qa_run["status"] == "failed"
    assert qa_run["result"]["blocker"]["category"] == QABlockerCategory.QA_ACCESS_GRANT_FAILED.value
    assert await qa_messages_for(redis_client, qa_run_id) == 0


@pytest.mark.asyncio
async def test_confirmed_grant_releases_qa_exactly_once(async_client, api_client, redis_client):
    project_id, application_id, qa_run_id = await target_with_qa_run(async_client)
    await start_grant(api_client, redis_client, project_id, application_id, qa_run_id)
    grant = await grant_for(async_client, qa_run_id)
    # Only an unskipped run that read the access back active records SUCCESS.
    await complete_operation(async_client, grant["grant_run_id"], application_id, None)

    await supervise_temporary_access(api_client, redis_client)
    await supervise_temporary_access(api_client, redis_client)

    grant = await grant_for(async_client, qa_run_id)
    assert grant["status"] == TemporaryAccessStatus.GRANTED.value
    assert grant["qa_dispatched_at"] is not None
    assert await qa_messages_for(redis_client, qa_run_id) == 1


async def _qa_run_on(client: RawAPI, project_id: str, application_id: int) -> str:
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


async def _settle_grant_through_to_granted(
    async_client, api_client, redis_client, project_id: str, application_id: int, qa_run_id: str
) -> None:
    await start_grant(api_client, redis_client, project_id, application_id, qa_run_id)
    grant = await grant_for(async_client, qa_run_id)
    await complete_operation(async_client, grant["grant_run_id"], application_id, None)
    await supervise_temporary_access(api_client, redis_client)
    assert (await grant_for(async_client, qa_run_id))[
        "status"
    ] == TemporaryAccessStatus.GRANTED.value


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
    project_id, application_id, first_qa_run_id = await target_with_qa_run(async_client)
    await _settle_grant_through_to_granted(
        async_client, api_client, redis_client, project_id, application_id, first_qa_run_id
    )

    # The first story is over: its QA reached a verdict.
    await finish_qa_run(async_client, first_qa_run_id)
    await supervise_temporary_access(api_client, redis_client)
    first = await grant_for(async_client, first_qa_run_id)
    assert first["status"] == TemporaryAccessStatus.REVOKING.value
    assert first["revoke_reason"] == TemporaryAccessRevokeReason.RUN_TERMINAL.value

    # The capability revoke proves the access inactive, and nothing else about
    # that redeploy may turn the proof into a failure.
    await complete_operation(async_client, first["revoke_run_id"], application_id, None)
    await supervise_temporary_access(api_client, redis_client)
    assert (await grant_for(async_client, first_qa_run_id))[
        "status"
    ] == TemporaryAccessStatus.REVOKED.value

    second_qa_run_id = await _qa_run_on(async_client, project_id, application_id)
    await start_grant(api_client, redis_client, project_id, application_id, second_qa_run_id)

    second = await grant_for(async_client, second_qa_run_id)
    assert second["status"] == TemporaryAccessStatus.GRANTING.value
    assert second["target_application_id"] == application_id
    assert second["id"] != first["id"]

    # Leave no queued paid run behind: the ceiling this suite shares is global.
    await finish_qa_run(async_client, second_qa_run_id)


@pytest.mark.asyncio
async def test_a_revoke_that_lands_after_undeploy_closes_the_grant(
    async_client, api_client, redis_client
):
    """Stand run 36075631307: the suite undeployed before the revoke ran.

    The deploy consumer now allocates nothing for that revoke and records what a
    proved revoke records; the real API has to accept it as revoke proof, and
    the grant closes with no retry and no escalation.
    """
    project_id, application_id, qa_run_id = await target_with_qa_run(async_client)
    await _settle_grant_through_to_granted(
        async_client, api_client, redis_client, project_id, application_id, qa_run_id
    )
    await finish_qa_run(async_client, qa_run_id)
    undeployed = await async_client.patch(
        f"/api/applications/{application_id}", json={"status": "not_deployed"}
    )
    assert undeployed.status_code == httpx.codes.OK, undeployed.text
    await supervise_temporary_access(api_client, redis_client)
    revoking = await grant_for(async_client, qa_run_id)
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

    grant = await grant_for(async_client, qa_run_id)
    assert grant["status"] == TemporaryAccessStatus.REVOKED.value
    assert grant["revoke_run_id"] == revoking["revoke_run_id"]
    assert grant["revoke_attempts"] == revoking["revoke_attempts"]
    assert grant["escalated_at"] is None


@pytest.mark.asyncio
async def test_a_live_first_grant_still_refuses_the_second_story(
    async_client, api_client, redis_client
):
    """The 409 guard stays: two live grants on one application are still refused."""
    project_id, application_id, first_qa_run_id = await target_with_qa_run(async_client)
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
    assert (await grant_for(async_client, first_qa_run_id))[
        "status"
    ] == TemporaryAccessStatus.GRANTED.value
    # Inside its bound the refusal settles nothing: the QA run stays queued.
    second_run = (await async_client.get(f"/api/runs/{second_qa_run_id}")).json()
    assert second_run["status"] == "queued"

    # Leave no queued paid run behind: the ceiling this suite shares is global.
    await finish_qa_run(async_client, second_qa_run_id)
    await finish_qa_run(async_client, first_qa_run_id)
