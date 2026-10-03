"""Credential authority is decided before minting, from live ownership."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fakeredis.aioredis import FakeRedis
from fastapi import HTTPException
import pytest

from shared.contracts.worker_control_plane import GitHubCredentialRequest
from src.config import WorkerManagerSettings
from src.routers.credentials import github_credential


@pytest.mark.parametrize("key", ["GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_PATH"])
def test_platform_minting_credentials_are_required(key, monkeypatch):
    monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match=key):
        WorkerManagerSettings(_env_file=None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "token",
        "missing_token",
        "qa",
        "missing",
        "malformed",
        "malformed_story",
        "unknown_type",
        "project",
        "repo",
        "removed",
    ],
)
async def test_denial_precedes_mint(failure):
    redis = FakeRedis(decode_responses=True)
    await redis.hset(
        "worker:broker:w",
        mapping={
            "token_digest": hashlib.sha256(
                b"" if failure == "missing_token" else b"broker"
            ).hexdigest()
        },
    )
    meta = {
        "worker_type": "developer",
        "project_id": "00000000-0000-0000-0000-000000000001",
        "repo_id": "r",
        "run_id": "run",
        "attempt_id": "a",
    }
    if failure == "qa":
        meta["worker_type"] = "qa"
    if failure == "missing":
        del meta["project_id"]
    if failure == "malformed":
        meta["attempt_id"] = ""
    if failure == "malformed_story":
        meta["story_id"] = ""
    if failure == "unknown_type":
        meta["worker_type"] = "unknown"
    await redis.hset("worker:meta:w", mapping=meta)
    api = AsyncMock()
    api.request.return_value = MagicMock()
    api.request.return_value.json.return_value = {
        "id": "r",
        "project_id": "00000000-0000-0000-0000-000000000002"
        if failure == "project"
        else "00000000-0000-0000-0000-000000000001",
        "git_url": "https://github.com/org/repo.git",
        "name": "repo",
        "role": "primary",
        "visibility": "private",
        "is_managed": True,
        "created_at": "2026-10-03T00:00:00Z",
    }
    if failure == "removed":
        api.request.side_effect = RuntimeError("removed")
    github = AsyncMock()
    github.get_repo_scoped_token.return_value = "synthetic-current"
    req = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(redis=redis, credential_api=api, github=github))
    )
    with pytest.raises(HTTPException):
        await github_credential(
            "w",
            GitHubCredentialRequest(repository="other/repo" if failure == "repo" else "org/repo"),
            req,
            None if failure == "missing_token" else "wrong" if failure == "token" else "broker",
        )
    github.get_repo_scoped_token.assert_not_awaited()


def test_request_cannot_choose_worker_or_project():
    for identity in ({"worker_id": "other"}, {"project_id": "other"}):
        with pytest.raises(ValueError):
            GitHubCredentialRequest.model_validate({"repository": "org/repo", **identity})


@pytest.mark.asyncio
async def test_scoped_mint_uses_owned_repository():
    redis = FakeRedis(decode_responses=True)
    await redis.hset(
        "worker:broker:w", mapping={"token_digest": hashlib.sha256(b"broker").hexdigest()}
    )
    await redis.hset(
        "worker:meta:w",
        mapping={
            "worker_type": "developer",
            "project_id": "00000000-0000-0000-0000-000000000001",
            "repo_id": "r",
            "run_id": "run",
            "attempt_id": "a",
        },
    )
    api = AsyncMock()
    api.request.return_value = MagicMock()
    api.request.return_value.json.return_value = {
        "id": "r",
        "project_id": "00000000-0000-0000-0000-000000000001",
        "git_url": "https://github.com/org/repo.git",
        "name": "repo",
        "role": "primary",
        "visibility": "private",
        "is_managed": True,
        "created_at": "2026-10-03T00:00:00Z",
    }
    github = AsyncMock()
    github.get_repo_scoped_token.return_value = "synthetic-current"
    req = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(redis=redis, credential_api=api, github=github))
    )
    result = await github_credential(
        "w", GitHubCredentialRequest(repository="org/repo"), req, "broker"
    )
    assert json.loads(result.body)["token"] == "synthetic-current"  # noqa: S105
    assert result.headers["cache-control"] == "no-store"
    github.get_repo_scoped_token.assert_awaited_once_with("org", "repo")
