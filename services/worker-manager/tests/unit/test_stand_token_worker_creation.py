"""A stand-token developer worker must reach container creation.

These tests build the real `WorkerManager` composition — its collaborators are
the ones `__init__` makes, not stand-ins — because the defect this covers was a
call to `self._stand_token_failures()` that no longer existed on the class. A
patched collaborator or a patched attribute would have hidden it.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.queues.worker import WorkerOwnership
from shared.contracts.vocab import AgentType
from src.manager import WorkerManager

_OWNERSHIP = WorkerOwnership(
    project_id="proj-stand",
    run_id="eng-363cc7a2792f",
    attempt_id="attempt-eng-363cc7a2792f",
)


def _make_docker_mock():
    docker = MagicMock()
    docker.image_exists = AsyncMock(return_value=True)
    docker.get_image_label = AsyncMock(return_value="basehash0001")
    docker.run_container = AsyncMock(return_value=MagicMock(id="container-stand"))
    docker.create_network = AsyncMock()
    docker.connect_network = AsyncMock()
    docker.remove_container = AsyncMock()
    docker.remove_network = AsyncMock()
    docker.get_container_logs = AsyncMock(return_value="logs")
    return docker


def _make_redis_mock():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.hgetall = AsyncMock(return_value={})
    redis.sismember = AsyncMock(return_value=False)
    redis.scan_iter = MagicMock(return_value=_empty_scan())
    return redis


async def _empty_scan(*args, **kwargs):
    for _ in ():
        yield _


@pytest.fixture
def valid_stand_claude_token(monkeypatch):
    """A Claude stand token the shared validator accepts, value-free."""
    import src.executor_diagnostics as diagnostics_module

    expires_at = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    monkeypatch.setattr(
        diagnostics_module.settings, "STAND_CLAUDE_CODE_OAUTH_TOKEN", "stand-claude-token", raising=False
    )
    monkeypatch.setattr(
        diagnostics_module.settings, "STAND_CLAUDE_CODE_OAUTH_TOKEN_EXPIRES_AT", expires_at, raising=False
    )


@pytest.mark.asyncio
@patch("src.manager.git_ops.refresh_git_token", new_callable=AsyncMock, return_value=True)
@patch("src.manager.workspace_mod")
@patch("src.manager.ImageBuilder")
async def test_stand_token_claude_worker_reaches_container_creation(
    mock_builder_cls, mock_workspace, mock_refresh, valid_stand_claude_token
):
    """The admitted engineering run gets a container, not an AttributeError."""
    mock_builder = MagicMock()
    mock_builder.get_image_tag.return_value = "worker:test"
    mock_builder.generate_dockerfile.return_value = "FROM base"
    mock_builder_cls.return_value = mock_builder
    mock_workspace.get_scaffolded_workspace.return_value = (Path("/data/workspaces/repo-stand"), True)
    docker = _make_docker_mock()

    manager = WorkerManager(redis=_make_redis_mock(), docker_client=docker)

    await manager.create_worker_with_capabilities(
        worker_id="w-stand-1",
        capabilities=["git"],
        base_image="worker-base:latest",
        ownership=_OWNERSHIP,
        agent_type=AgentType.CLAUDE,
        auth_mode="stand_token",
        env_vars={"GITHUB_TOKEN": "tok", "REPO_NAME": "org/repo"},
        repo_id="repo-stand",
    )

    docker.run_container.assert_awaited()


@pytest.mark.asyncio
@patch("src.manager.workspace_mod")
@patch("src.manager.ImageBuilder")
async def test_stand_token_claude_worker_refuses_on_an_unusable_token(mock_builder_cls, mock_workspace, monkeypatch):
    """The same reading still refuses, so the fix did not drop the check."""
    import src.executor_diagnostics as diagnostics_module

    mock_builder_cls.return_value = MagicMock()
    mock_workspace.get_scaffolded_workspace.return_value = (Path("/data/workspaces/repo-stand"), True)
    monkeypatch.setattr(diagnostics_module.settings, "STAND_CLAUDE_CODE_OAUTH_TOKEN", None, raising=False)
    monkeypatch.setattr(diagnostics_module.settings, "STAND_CLAUDE_CODE_OAUTH_TOKEN_EXPIRES_AT", None, raising=False)
    docker = _make_docker_mock()

    manager = WorkerManager(redis=_make_redis_mock(), docker_client=docker)

    with pytest.raises(RuntimeError, match="stand_token authentication is unavailable"):
        await manager.create_worker_with_capabilities(
            worker_id="w-stand-2",
            capabilities=["git"],
            base_image="worker-base:latest",
            ownership=_OWNERSHIP,
            agent_type=AgentType.CLAUDE,
            auth_mode="stand_token",
            env_vars={"GITHUB_TOKEN": "tok", "REPO_NAME": "org/repo"},
            repo_id="repo-stand",
        )

    docker.run_container.assert_not_awaited()
