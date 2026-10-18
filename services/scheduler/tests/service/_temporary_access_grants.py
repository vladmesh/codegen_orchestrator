"""A real grant target, its QA run and its capability operations for the scheduler service tests."""

import uuid

import httpx

from shared.contracts.dto.run_result import DeployRunResult, DeploySkipReason, QARunResult
from shared.contracts.dto.temporary_access import TemporaryAccessStatus
from shared.contracts.queues.deploy import DeployOutcome
from shared.contracts.queues.qa import QAMessage, QAOutcome
from shared.tests.ssh_key_fixtures import fleet_private_key
from src.tasks.temporary_access import grant_temporary_access

HEAD_SHA = "c" * 40
BUILT_SHA = "d" * 40
TARGET_URL = "https://grant-proof.example.com"


class RawAPI:
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


class OnlyGrant:
    """The real API client, with the temporary-access watch list narrowed to one grant.

    The service database is shared by every test in the session, so a sweep must not
    reconcile another test's grants. Every other call is the real client's.
    """

    def __init__(self, api_client, grant_id: str) -> None:
        self._api = api_client
        self._grant_id = grant_id

    async def list_temporary_access_grants_under_watch(self) -> list:
        grants = await self._api.list_temporary_access_grants_under_watch()
        return [grant for grant in grants if grant.id == self._grant_id]

    def __getattr__(self, name: str):
        return getattr(self._api, name)


async def target_with_qa_run(client: RawAPI) -> tuple[str, int, str]:
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


async def grant_for(client: RawAPI, qa_run_id: str) -> dict:
    """The one grant record of this QA run."""
    rows = (
        await client.get("/api/temporary-access-grants/", params={"qa_run_id": qa_run_id})
    ).json()
    assert len(rows) == 1, rows
    return rows[0]


async def complete_operation(
    client: RawAPI, run_id: str, application_id: int, skipped_reason: DeploySkipReason | None
) -> None:
    """Record a grant or revoke operation's `SUCCESS` as the deploy consumer does."""
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


async def start_grant(
    api_client, redis_client, project_id: str, application_id: int, qa_run_id: str
) -> None:
    """Open a GRANTING record and queue its first grant operation."""
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


async def finish_qa_run(client: RawAPI, qa_run_id: str) -> None:
    """The QA verdict that ends the story and releases its access."""
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
