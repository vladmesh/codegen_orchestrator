"""Recovery execution ownership and transient scoped credential boundary."""

from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fakeredis.aioredis import FakeRedis
import pytest

from shared.contracts.dto.commit_publication import CommitPublication, PublicationFailure
from src.routers import commit_recovery

# Every test here starts processes: CI runs this file, the host profile skips it.
pytestmark = pytest.mark.subprocess

PROJECT = "00000000-0000-0000-0000-000000000001"


def git(path, *args):
    return subprocess.run(
        ["/usr/bin/git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def owned_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "fixture-internal")
    work = tmp_path / "repo-owned"
    work.mkdir()
    git(work, "init", "--initial-branch=story/story-owned")
    git(work, "config", "user.name", "Fixture")
    git(work, "config", "user.email", "fixture@example.test")
    git(work, "remote", "add", "origin", "https://github.com/fixture/owned.git")
    (work / "app.txt").write_text("base")
    git(work, "add", "app.txt")
    git(work, "commit", "-m", "base")
    baseline = git(work, "rev-parse", "HEAD")
    (work / "app.txt").write_text("change")
    git(work, "commit", "-am", "change")
    sha = git(work, "rev-parse", "HEAD")
    identity = {
        "worker_id": "worker-owned",
        "attempt_id": "eng-owned",
        "story_id": "story-owned",
        "project_id": PROJECT,
        "initiating_run_id": "init-owned",
        "repository_id": "repo-owned",
        "repository_url": "https://github.com/fixture/owned.git",
        "branch": "story/story-owned",
        "baseline": baseline,
        "cycle": None,
        "iteration": 2,
    }
    claim = {
        "identity": identity,
        "commit_sha": sha,
        "attempt_id": "eng-owned",
        "story_id": "story-owned",
        "actor": "internal_service",
        "stop_id": None,
        "claimed_at": "2026-10-04T00:00:00Z",
        "receipt": None,
        "handed_off_at": None,
    }
    run = {
        "id": "eng-owned",
        "type": "engineering",
        "status": "failed",
        "project_id": PROJECT,
        "story_id": "story-owned",
        "run_metadata": {"worker_id": "worker-owned"},
        "result": {"engineering_status": "failed"},
        "created_at": "2026-10-04T00:00:00Z",
    }
    repository = {
        "id": "repo-owned",
        "project_id": PROJECT,
        "name": "owned",
        "role": "primary",
        "git_url": identity["repository_url"],
        "visibility": "private",
        "is_managed": True,
        "created_at": "2026-10-04T00:00:00Z",
    }
    api = AsyncMock()

    async def request(method, path):
        assert method == "GET"
        rows = {
            "commit-recoveries/eng-owned": claim,
            "runs/eng-owned": run,
            "repositories/repo-owned": repository,
        }
        response = MagicMock()
        response.json.return_value = rows[path]
        return response

    api.request.side_effect = request
    github = AsyncMock()
    github.get_repo_scoped_token.side_effect = ["synthetic-fresh-one", "synthetic-fresh-two"]
    state = SimpleNamespace(
        credential_api=api,
        github=github,
        redis=FakeRedis(decode_responses=True),
        scaffolded_workspace_path=str(tmp_path),
        fixture_claim=claim,
    )
    return SimpleNamespace(app=SimpleNamespace(state=state)), run, repository


async def test_recovery_reacquires_scoped_token_and_never_starts_executor(
    owned_checkout, monkeypatch
):
    req, _, _ = owned_checkout
    calls = []

    def native(workspace, branch, sha, **kwargs):
        calls.append(kwargs)
        assert workspace != Path(req.app.state.scaffolded_workspace_path) / "repo-owned"
        assert branch == "story/story-owned" and sha == req.app.state.fixture_claim["commit_sha"]
        assert kwargs["baseline"] == req.app.state.fixture_claim["identity"]["baseline"]
        assert kwargs["env"]["GIT_CONFIG_KEY_2"] == "credential.helper"
        assert kwargs["env"]["GIT_CONFIG_VALUE_2"] == ""
        assert "GITHUB_TOKEN" not in kwargs["env"]
        return CommitPublication(published=True, commit_sha=sha, remote_sha=sha, branch=branch)

    monkeypatch.setattr(commit_recovery, "publish_commit", native)
    for _ in range(2):
        receipt = await commit_recovery.publish_preserved_commit(
            "eng-owned", req, "fixture-internal"
        )
        assert receipt.published and receipt.attempt_id == "eng-owned"
    assert calls[0]["env"]["GIT_CONFIG_VALUE_1"] != calls[1]["env"]["GIT_CONFIG_VALUE_1"]
    assert req.app.state.github.get_repo_scoped_token.await_args_list[0].args == (
        "fixture",
        "owned",
    )
    # The request has no container/manager/executor capability, and the checkout
    # has no credential files after both native calls.
    assert list(Path(req.app.state.scaffolded_workspace_path).rglob(".git-credentials")) == []


async def test_replaced_ownership_refuses_before_token(owned_checkout):
    req, run, _ = owned_checkout
    run["run_metadata"]["worker_id"] = "replacement"
    receipt = await commit_recovery.publish_preserved_commit("eng-owned", req, "fixture-internal")
    assert receipt.failure is PublicationFailure.OWNERSHIP_MISSING
    req.app.state.github.get_repo_scoped_token.assert_not_awaited()


async def test_live_checkout_refuses_without_token_or_reset(owned_checkout):
    req, _, _ = owned_checkout
    await req.app.state.redis.set(f"workspace:lock:{PROJECT}", "worker-owned")
    receipt = await commit_recovery.publish_preserved_commit("eng-owned", req, "fixture-internal")
    assert receipt.failure is PublicationFailure.STALE_ATTEMPT
    req.app.state.github.get_repo_scoped_token.assert_not_awaited()
