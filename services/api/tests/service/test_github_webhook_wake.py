"""A signed GitHub workflow completion over HTTP publishes the PR/CI hint on real Redis."""

from collections.abc import AsyncGenerator
import hashlib
import hmac
import json
import os

from httpx import ASGITransport, AsyncClient
import pytest

from shared.contracts.queues.scheduler_wake import SchedulerWakeTarget

PATH = "/api/webhooks/github"


def sign(body: bytes, key: str) -> str:
    return "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


def completed(action: str = "completed", status: str = "completed") -> bytes:
    run = {"id": 30433642, "status": status, "conclusion": "failure", "head_branch": "story/x"}
    return json.dumps({"action": action, "workflow_run": run, "repository": {"id": 1}}).encode()


@pytest.fixture
async def github() -> AsyncGenerator[AsyncClient, None]:
    """A caller holding no API credential, as GitHub is."""
    from src.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def deliver(client: AsyncClient, body: bytes, event: str, **headers: str):
    return await client.post(
        PATH,
        content=body,
        headers={"Content-Type": "application/json", "X-GitHub-Event": event, **headers},
    )


@pytest.mark.asyncio
async def test_a_signed_completion_publishes_exactly_one_pr_ci_hint(github, wake_hints):
    body = completed()
    signature = sign(body, os.environ["GITHUB_WEBHOOK_SECRET"])

    response = await deliver(github, body, "workflow_run", **{"X-Hub-Signature-256": signature})

    assert response.status_code == 204, response.text
    assert response.content == b""
    assert await wake_hints.published() == [SchedulerWakeTarget.PR_CI]

    replay = await deliver(github, body, "workflow_run", **{"X-Hub-Signature-256": signature})

    assert replay.status_code == 204, replay.text
    assert await wake_hints.published() == [SchedulerWakeTarget.PR_CI]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credentials",
    [
        {},
        {"X-Hub-Signature-256": sign(completed(), "not-the-webhook-secret")},
        {"X-Hub-Signature-256": sign(completed(), os.environ["INTERNAL_API_KEY"])},
        {
            "X-Internal-Key": os.environ["INTERNAL_API_KEY"],
            "Authorization": "Bearer not-a-signature",
        },
    ],
    ids=["unsigned", "wrong-secret", "internal-key-as-secret", "api-credentials"],
)
async def test_bad_authentication_is_refused_and_publishes_nothing(github, wake_hints, credentials):
    response = await deliver(github, completed(), "workflow_run", **credentials)

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "Invalid webhook signature"}
    assert await wake_hints.published() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "body"),
    [
        ("ping", json.dumps({"zen": "Design for failure.", "hook_id": 1}).encode()),
        ("workflow_run", completed(action="in_progress", status="in_progress")),
    ],
    ids=["ping", "ignored-action"],
)
async def test_a_signed_ping_or_ignored_action_is_acknowledged_silently(
    github, wake_hints, event, body
):
    signature = sign(body, os.environ["GITHUB_WEBHOOK_SECRET"])

    response = await deliver(github, body, event, **{"X-Hub-Signature-256": signature})

    assert response.status_code == 204, response.text
    assert await wake_hints.published() == []


@pytest.mark.asyncio
async def test_a_webhook_signature_opens_no_neighbouring_route(github, wake_hints):
    body = json.dumps({"project_id": "00000000-0000-0000-0000-000000000001"}).encode()
    signature = sign(body, os.environ["GITHUB_WEBHOOK_SECRET"])

    for method, path in (("POST", "/api/stories/"), ("GET", "/api/stories/")):
        response = await github.request(
            method,
            path,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "workflow_run",
                "X-Hub-Signature-256": signature,
            },
        )
        assert response.status_code == 401, (path, response.text)

    assert await wake_hints.published() == []
