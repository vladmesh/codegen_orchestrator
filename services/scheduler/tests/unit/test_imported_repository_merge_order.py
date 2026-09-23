"""A freshly imported product repository: secrets, merge, build, images, deploy, in order.

Fortune Teller Bot's merge started push-main CI at 23:02:15 while the repository
still held an old, unresolvable registry hostname; the deploy worker wrote the
current one at 23:02:31, after the builds had read the stale one. Both image
builds failed and the deploy found no images.

The GitHub double below is one imported repository. Its secret writes go through
the real ``GitHubAppClient.refresh_registry_secrets`` and ``set_repository_secrets``;
a merge starts a push-main CI run that reads the registry secrets when it starts,
as the product template's ``build-and-push`` job does, and publishes images only
if those values reach the orchestrator's real registry.
"""

from __future__ import annotations

from datetime import UTC, datetime
import os
from unittest.mock import AsyncMock, patch

import pytest
from structlog.testing import capture_logs

from shared.clients.github import SecretsMixin
from shared.queues import DEPLOY_QUEUE
from src.tasks.pr_poller import poll_merged_prs

REGISTRY_ENV = {
    "ORCHESTRATOR_HOSTNAME": "registry.current.example.com",
    "REGISTRY_USER": "registry-user",
    "REGISTRY_PASSWORD": "current-registry-password",  # noqa: S105
}
STALE_SECRETS = {
    "REGISTRY_URL": "registry.retired.example.com",
    "REGISTRY_PASSWORD": "retired-registry-password",  # noqa: S105
}
REGISTRY_SECRETS = ("REGISTRY_URL", "REGISTRY_USER", "REGISTRY_PASSWORD")
MERGE_SHA = "e" * 40
HEAD_SHA = "a" * 40


class ImportedRepository(SecretsMixin):
    """One product repository on GitHub that this platform did not create."""

    def __init__(self, events: list, *, refused_secret: str | None = None) -> None:
        self.events = events
        self.refused_secret = refused_secret
        self.secrets = dict(STALE_SECRETS)
        self.pull_request = {
            "number": 42,
            "state": "open",
            "merged_at": None,
            "auto_merge": None,
            "mergeable_state": "clean",
            "head": {"sha": HEAD_SHA},
        }
        self.ci_run: dict | None = None
        self.published_images: list[str] = []

    async def __aenter__(self) -> ImportedRepository:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def set_repository_secret(  # noqa: PLR0913 — the real method's signature
        self, owner: str, repo: str, secret_name: str, secret_value: str, token=None
    ) -> None:
        if secret_name == self.refused_secret:
            raise RuntimeError(f"403 Resource not accessible by integration: {secret_value}")
        self.secrets[secret_name] = secret_value
        self.events.append(("secret_written", secret_name))

    async def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict:
        return dict(self.pull_request)

    async def merge_pull_request(self, owner: str, repo: str, pr_number: int) -> dict:
        self.events.append(("merged", MERGE_SHA))
        self.pull_request.update(
            state="closed",
            merged_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            merge_commit_sha=MERGE_SHA,
            mergeable_state=None,
        )
        # Push-main CI starts on the merge and its build jobs read the secrets now.
        login = {name: self.secrets.get(name) for name in REGISTRY_SECRETS}
        self.ci_run = {
            "id": 900,
            "status": "in_progress",
            "conclusion": None,
            "html_url": "https://github.com/org/imported-repo/actions/runs/900",
            "head_sha": MERGE_SHA,
            "login": login,
        }
        self.events.append(("ci_started", login["REGISTRY_URL"]))
        return {"merged": True, "sha": MERGE_SHA}

    def finish_build(self) -> None:
        """``build-and-push`` logs in with what it read at start, then pushes or fails."""
        login = self.ci_run["login"]
        reachable = login == {
            "REGISTRY_URL": REGISTRY_ENV["ORCHESTRATOR_HOSTNAME"],
            "REGISTRY_USER": REGISTRY_ENV["REGISTRY_USER"],
            "REGISTRY_PASSWORD": REGISTRY_ENV["REGISTRY_PASSWORD"],
        }
        if reachable:
            self.published_images.append(
                f"{login['REGISTRY_URL']}/org/imported-repo/backend:sha-{MERGE_SHA[:7]}"
            )
        self.ci_run.update(status="completed", conclusion="success" if reachable else "failure")

    async def get_latest_workflow_run(  # noqa: PLR0913 — the real method's signature
        self,
        owner,
        repo,
        workflow_file="main.yml",
        branch="main",
        created_after=None,
        head_sha=None,
    ) -> dict | None:
        run = self.ci_run
        if run is None or (workflow_file, branch, head_sha) != ("ci.yml", "main", run["head_sha"]):
            return None
        if run["conclusion"] == "success":
            self.events.append(("images_observed", run["head_sha"]))
        return {key: value for key, value in run.items() if key != "login"}

    async def get_workflow_failure_details(self, owner, repo, run_id, log_excerpt_lines=None):
        return {"failed_jobs": [], "unavailable_reason": "not modelled"}


def _api() -> AsyncMock:
    story = AsyncMock()
    story.id = "story-1"
    story.project_id = "proj-1"
    story.pr_number = 42
    story.generated_product_timeline = None
    repository = AsyncMock()
    repository.git_url = "https://github.com/org/imported-repo"
    api = AsyncMock()
    api.get_stories_by_status.return_value = [story]
    api.get_primary_repository.return_value = repository
    api.get_stories_by_project.return_value = []
    return api


def _redis(events: list) -> AsyncMock:
    redis = AsyncMock()

    async def publish(queue, message):
        if queue == DEPLOY_QUEUE:
            events.append(("deploy_dispatched", message.deployed_commit_sha))

    redis.publish_message.side_effect = publish
    return redis


@pytest.mark.asyncio
@patch.dict(os.environ, REGISTRY_ENV)
async def test_secrets_then_merge_then_build_then_images_then_deploy():
    events: list = []
    github = ImportedRepository(events)
    api, redis = _api(), _redis(events)

    with patch("src.tasks.pr_poller.GitHubAppClient", return_value=github):
        # Tick 1 merges; the build it started is still running, so nothing deploys.
        assert await poll_merged_prs(api, redis) == 0
        github.finish_build()
        # Tick 2 sees the merge commit's images and only then dispatches the deploy.
        assert await poll_merged_prs(api, redis) == 1

    assert events == [
        ("secret_written", "REGISTRY_URL"),
        ("secret_written", "REGISTRY_USER"),
        ("secret_written", "REGISTRY_PASSWORD"),
        ("merged", MERGE_SHA),
        ("ci_started", "registry.current.example.com"),
        ("images_observed", MERGE_SHA),
        ("deploy_dispatched", MERGE_SHA),
    ]
    assert github.published_images == [
        f"registry.current.example.com/org/imported-repo/backend:sha-{MERGE_SHA[:7]}"
    ]


@pytest.mark.asyncio
@patch.dict(os.environ, REGISTRY_ENV)
async def test_without_the_refresh_the_same_repository_builds_nothing():
    """The double reproduces the incident, so the test above is not vacuous."""
    events: list = []
    github = ImportedRepository(events)
    await github.merge_pull_request("org", "imported-repo", 42)
    github.finish_build()

    assert events[-1] == ("ci_started", "registry.retired.example.com")
    assert github.ci_run["conclusion"] == "failure"
    assert github.published_images == []


@pytest.mark.asyncio
@patch.dict(os.environ, REGISTRY_ENV)
@patch("src.tasks.pr_poller.notify_admins_best_effort", new_callable=AsyncMock)
@patch("src.tasks.pr_poller.deliver_owed_notification", new_callable=AsyncMock)
@patch("src.tasks.pr_poller.owe_story_owner_notification", new_callable=AsyncMock)
async def test_a_refresh_that_fails_leaves_the_pull_request_unmerged(owe, deliver, notify):
    events: list = []
    github = ImportedRepository(events, refused_secret="REGISTRY_PASSWORD")  # noqa: S106
    api, redis = _api(), _redis(events)

    with (
        patch("src.tasks.pr_poller.GitHubAppClient", return_value=github),
        capture_logs() as logs,
    ):
        assert await poll_merged_prs(api, redis) == 0

    assert ("merged", MERGE_SHA) not in events
    assert github.ci_run is None
    assert github.pull_request["state"] == "open"
    reason = api.update_story.await_args.args[1]["quarantine_reason"]
    assert reason["reason"] == "registry_secrets_write_incomplete"
    assert reason["detail"].endswith("wrote 2 of 3 registry secrets to org/imported-repo")
    api.transition_story.assert_awaited_once_with("story-1", "human-review")
    redis.publish_message.assert_not_awaited()
    assert REGISTRY_ENV["REGISTRY_PASSWORD"] not in repr(logs)
    assert REGISTRY_ENV["REGISTRY_PASSWORD"] not in repr(api.update_story.await_args_list)
