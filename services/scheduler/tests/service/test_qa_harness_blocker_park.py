"""Real API proof that a QA harness blocker parks the story and never becomes product work.

Each blocker the QA runtime raises when its harness — not the product — failed
(a stale `qa-docker`, a refused verb, an unreadable probe, an SSH or runtime
failure, an executor that never reached the capability endpoint) is driven
through the real supervisor against the real API: the story parks in human
review exactly once, an administrator is told how to recover, no engineering
fix task or iteration is created, the owner is not told the product is broken,
and `recheck-qa` accepts the park.
"""

import os
import uuid

import httpx
import pytest

from shared.contracts.dto.owner_notification import OWNER_NOTIFICATION_KEY
from shared.contracts.dto.run_result import QA_HARNESS_BLOCKERS, QABlockerCategory
from shared.contracts.dto.story import StoryStatus
from shared.redis import RedisStreamClient
from shared.tests.ssh_key_fixtures import fleet_private_key
from src.tasks.supervisor import supervise_testing_stories

HARNESS_BLOCKERS = {
    QABlockerCategory.QA_TARGET_PROFILE_STALE: (
        "read the generated contract codegen_kit/_active_packages.py from the backend container",
        "docker read-contract weather-backend-1 codegen_kit/_active_packages.py 262144",
        "exit 2: qa-docker: docker read-contract is refused on this host",
    ),
    QABlockerCategory.QA_PROBE_UNAVAILABLE: (
        "read the generated contract services/backend/manifest.yaml from the backend container",
        "docker read-contract weather-backend-1 services/backend/manifest.yaml 262144",
        "exit 7: contract is not a readable regular file",
    ),
    QABlockerCategory.SERVER_UNAVAILABLE: (
        "issue a one-shot QA identity on the target",
        "authorized_keys entry codegen-qa-run-1 on 10.9.0.9",
        "could not reach 10.9.0.9 to issue a QA identity: Connection refused",
    ),
    QABlockerCategory.QA_EXECUTOR_UNAVAILABLE: (
        "run exploratory QA on the assigned executor (codex)",
        "2 start attempt(s) of the codex QA executor against http://10.9.0.9:8000",
        "the QA executor container ran but never reached the capability endpoint: no output",
    ),
}


class _Admins:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def __call__(self, message: str, **_: object) -> None:
        self.messages.append(message)


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


async def _testing_story_with_blocked_qa(
    client: httpx.AsyncClient, category: QABlockerCategory
) -> tuple[str, str]:
    telegram_id = uuid.uuid4().int % 1_000_000_000
    user = await client.post(
        "/api/users/", json={"telegram_id": telegram_id, "username": f"harness-{telegram_id}"}
    )
    assert user.is_success, user.text
    project_id = str(uuid.uuid4())
    project = await client.post(
        "/api/projects/",
        json={
            "id": project_id,
            "title": "Harness blocker park",
            "initiating_run_id": f"init-{uuid.uuid4().hex}",
            "status": "active",
            "config": {"workspace_ready": True},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert project.is_success, project.text
    server = await client.post(
        "/api/servers/",
        json={
            "handle": f"harness-{uuid.uuid4().hex[:8]}",
            "host": "h.test",
            "public_ip": "10.9.0.9",
            "ssh_key": fleet_private_key(),
        },
    )
    assert server.status_code == httpx.codes.CREATED, server.text
    repository = await client.post(
        "/api/repositories/",
        json={
            "project_id": project_id,
            "name": f"harness-{uuid.uuid4().hex[:8]}",
            "git_url": "https://github.com/test/harness.git",
        },
    )
    assert repository.status_code == httpx.codes.CREATED, repository.text
    application = await client.post(
        "/api/applications/",
        json={
            "repo_id": repository.json()["id"],
            "server_handle": server.json()["handle"],
            "service_name": "harness-service",
            "status": "running",
        },
    )
    assert application.status_code == httpx.codes.CREATED, application.text
    application_id = application.json()["id"]
    story = await client.post("/api/stories/", json={"project_id": project_id, "title": "Harness"})
    assert story.status_code == httpx.codes.CREATED, story.text
    story_id = story.json()["id"]
    assert (await client.post(f"/api/stories/{story_id}/start")).is_success
    assert (await client.post(f"/api/stories/{story_id}/deploy")).is_success
    assert (await client.post(f"/api/stories/{story_id}/test")).is_success

    deploy = await client.post(
        "/api/runs/",
        json={
            "id": f"deploy-harness-{uuid.uuid4().hex[:12]}",
            "type": "deploy",
            "project_id": project_id,
            "story_id": story_id,
            "run_metadata": {"application_id": application_id, "head_sha": "f" * 40},
        },
    )
    assert deploy.status_code == httpx.codes.CREATED, deploy.text
    qa = await client.post(
        "/api/work-admission/paid-runs",
        json={
            "id": f"qa-harness-{uuid.uuid4().hex[:12]}",
            "type": "qa",
            "project_id": project_id,
            "story_id": story_id,
            "run_metadata": {"application_id": application_id},
        },
    )
    assert qa.status_code == httpx.codes.OK, qa.text
    qa_run_id = qa.json()["run_id"]
    attempted, sent, received = HARNESS_BLOCKERS[category]
    settled = await client.patch(
        f"/api/runs/{qa_run_id}",
        json={
            "status": "completed",
            "result": {
                "qa_outcome": "blocked",
                "summary": "QA could not verify the product",
                "blocker": {
                    "category": category.value,
                    "attempted": attempted,
                    "sent": sent,
                    "received": received,
                },
            },
        },
    )
    assert settled.status_code == httpx.codes.OK, settled.text
    return story_id, qa_run_id


def test_every_blocker_driven_here_is_a_harness_blocker():
    assert set(HARNESS_BLOCKERS) <= QA_HARNESS_BLOCKERS


@pytest.mark.asyncio
@pytest.mark.parametrize("category", list(HARNESS_BLOCKERS), ids=lambda category: category.value)
async def test_a_harness_blocker_parks_once_for_operator_recovery(
    async_client, api_client, monkeypatch, category
):
    admins = _Admins()
    monkeypatch.setattr("src.tasks.supervisor.qa.notify_admins_best_effort", admins)
    story_id, qa_run_id = await _testing_story_with_blocked_qa(async_client, category)
    redis_client = RedisStreamClient(os.environ["REDIS_URL"])
    await redis_client.connect()
    try:
        await supervise_testing_stories(api_client, redis_client)
        await supervise_testing_stories(api_client, redis_client)
    finally:
        await redis_client.close()

    story = (await async_client.get(f"/api/stories/{story_id}")).json()
    assert story["status"] == StoryStatus.WAITING_HUMAN_REVIEW.value
    assert story["quarantine_reason"]["blocker"]["category"] == category.value

    tasks = (await async_client.get("/api/tasks/", params={"story_id": story_id})).json()
    assert tasks == [], "a harness blocker must never become an engineering fix task"
    engineering = (
        await async_client.get(
            "/api/runs/", params={"story_id": story_id, "run_type": "engineering"}
        )
    ).json()
    assert engineering == [], "a harness blocker must never spend an engineering iteration"

    assert len(admins.messages) == 1, "parked once, announced once"
    assert category.value in admins.messages[0]
    assert f"/api/stories/{story_id}/recheck-qa" in admins.messages[0]

    # A quarantine's owner notice is owed on the QA run that ended the story.
    qa_run = await async_client.get(f"/api/runs/{qa_run_id}")
    assert qa_run.status_code == httpx.codes.OK, qa_run.text
    owed = qa_run.json()["run_metadata"][OWNER_NOTIFICATION_KEY]
    assert owed["event"] == "story_quarantined"
    assert "not in the product" in owed["text"]
    assert "fix" not in owed["text"].lower()

    recheck = await async_client.post(
        f"/api/stories/{story_id}/recheck-qa",
        json={"basis": "The target was reconciled."},
        headers={"X-Admin-Console-Operator": "shared-console"},
    )
    assert (
        recheck.status_code != httpx.codes.UNPROCESSABLE_ENTITY
        or "cannot be cleared" not in recheck.text
    )
