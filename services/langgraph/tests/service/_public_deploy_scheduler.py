"""Run the real scheduler in its own service import boundary for deploy regressions."""

import asyncio
from datetime import UTC, datetime, timedelta
import os
from unittest.mock import AsyncMock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt

from shared.clients.github import GitHubAppClient
from shared.redis import RedisStreamClient
from src.clients.api import SchedulerAPIClient
from src.tasks.owner_notifications import supervise_owed_owner_notifications
from src.tasks.pr_poller import poll_merged_prs
from src.tasks.supervisor.deploy import supervise_deploying_stories


class AuthenticatedGitHubFixture(GitHubAppClient):
    """Exercise App JWT/token exchange with deterministic HTTP publication evidence."""

    def __init__(self):
        super().__init__()
        self.app_id = "1438"
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key = key.public_key()
        self._private_key = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        self.calls = []

    async def __aenter__(self):
        self._http_client = httpx.AsyncClient(transport=httpx.MockTransport(self.respond))
        return self

    def respond(self, request):
        assert request.url.host == "api.github.com"
        path = request.url.path
        self.calls.append(path)
        if path in {
            "/repos/fixture/public/installation",
            "/app/installations/1438/access_tokens",
        }:
            authorization = request.headers["Authorization"]
            assert authorization.startswith("Bearer ")
            jwt.decode(
                authorization.removeprefix("Bearer "),
                self.public_key,
                algorithms=["RS256"],
                issuer=self.app_id,
            )
            if path.endswith("/installation"):
                assert request.method == "GET"
                payload = {"id": 1438}
            else:
                assert request.method == "POST"
                payload = {
                    "token": "fixture-installation-token",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                }
        else:
            assert request.method == "GET"
            assert request.headers["Authorization"] == "token fixture-installation-token"
            if path == f"/repos/fixture/public/pulls/{os.environ['PUBLIC_DEPLOY_PR']}":
                payload = {
                    "number": int(os.environ["PUBLIC_DEPLOY_PR"]),
                    "state": "closed",
                    "merged_at": datetime.now(UTC).isoformat(),
                    "head": {"sha": os.environ["PUBLIC_DEPLOY_HEAD"]},
                    "merge_commit_sha": os.environ["PUBLIC_DEPLOY_BUILT"],
                }
            else:
                assert path == "/repos/fixture/public/actions/workflows/ci.yml/runs"
                assert dict(request.url.params) == {
                    "branch": "main",
                    "per_page": "1",
                    "head_sha": os.environ["PUBLIC_DEPLOY_BUILT"],
                }
                payload = {
                    "workflow_runs": [
                        {
                            "id": 12345,
                            "status": "completed",
                            "conclusion": "success",
                            "html_url": "https://github.com/fixture/public/actions/runs/12345",
                            "created_at": datetime.now(UTC).isoformat(),
                            "head_sha": os.environ["PUBLIC_DEPLOY_BUILT"],
                        }
                    ]
                }
        return httpx.Response(200, json=payload)


async def run():
    api = SchedulerAPIClient()
    stream = RedisStreamClient()
    await stream.connect()
    get_stories = api.get_stories_by_status

    async def selected_stories(status):
        return [
            story
            for story in await get_stories(status)
            if story.id == os.environ["PUBLIC_DEPLOY_STORY"]
        ]

    api.get_stories_by_status = selected_stories
    try:
        mode = os.environ["PUBLIC_DEPLOY_MODE"]
        if mode == "poll":
            github = AuthenticatedGitHubFixture()
            with (
                patch("src.tasks.pr_poller.GitHubAppClient", return_value=github),
                patch("src.tasks.pr_poller._ci_failure_log_excerpt_lines", return_value=10),
            ):
                assert await poll_merged_prs(api, stream) == 1
            assert github.calls.count("/app/installations/1438/access_tokens") == 1
            assert github.calls.count("/repos/fixture/public/actions/workflows/ci.yml/runs") == 1
        elif mode == "fail":
            counts = await supervise_deploying_stories(api, stream)
            assert counts["failed"] == 1
        elif mode == "refuse":
            original = api.stop_story

            async def refused_stop(*args, **kwargs):
                with patch.object(api, "_internal_api_key", "invalid-test-internal-key"):
                    return await original(*args, **kwargs)

            api.stop_story = AsyncMock(side_effect=refused_stop)
            try:
                await supervise_deploying_stories(api, stream)
            except httpx.HTTPStatusError as refusal:
                assert refusal.response.status_code in {401, 403}
            else:
                raise AssertionError("refused stop did not propagate")
            api.stop_story.assert_awaited_once()
            api.stop_story = original
            story = await api.get_story(os.environ["PUBLIC_DEPLOY_STORY"])
            assert story.status.value == "deploying"
        elif mode in {"notify", "interrupt_notify"}:
            if mode == "interrupt_notify":
                stream.publish_flat = AsyncMock(
                    side_effect=ConnectionError("Redis publication interrupted")
                )
            with patch("src.tasks.owner_notifications.notify_admins_best_effort", AsyncMock()):
                await supervise_owed_owner_notifications(api, stream)
    finally:
        await api.close()
        await stream.close()


asyncio.run(run())
