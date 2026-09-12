"""A coding worker is handed a repository-scoped GitHub token, never an installation-wide one.

The worker is an ephemeral container with unrestricted egress: a prompt-injected
agent holding an installation-wide token reaches every repository the GitHub App
installation covers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.executor_decision import ExecutorDecision, ExecutorDecisionSource
from shared.contracts.dto.run import RunType
from shared.contracts.queues.worker import WorkerOwnership
from shared.contracts.vocab import AgentType
from tests.unit.factories import make_project, make_repository


def _state() -> dict:
    return {
        "project_spec": {
            "id": "proj-1",
            "name": "test-project",
            "status": "active",
            "config": {"description": "Test", "modules": ["backend"]},
        },
        "action": "feature",
        "run_id": "eng-1",
        "ownership": WorkerOwnership(project_id="proj-1", run_id="live-1", attempt_id="eng-1"),
        "executor_decision": ExecutorDecision(
            attempt_kind=RunType.ENGINEERING,
            agent_type=AgentType.CLAUDE,
            source=ExecutorDecisionSource.API_DEFAULT,
            policy_version="v1",
            reason="Engineering executor selected by API DEFAULT_AGENT_TYPE.",
        ),
        "description": "Add payment processing",
    }


@pytest.mark.asyncio
@patch("src.nodes.developer.api_client")
@patch("src.nodes.developer.GitHubAppClient")
@patch("src.nodes.developer.request_spawn")
async def test_worker_receives_repo_scoped_token(mock_spawn, mock_github_cls, mock_api):
    from src.clients.worker_spawner import SpawnResult
    from src.nodes.developer import DeveloperNode

    mock_spawn.return_value = SpawnResult(
        request_id="req-1",
        success=True,
        exit_code=0,
        output="done",
        commit_sha="abc123",
        worker_id="w-1",
    )
    mock_api.get_project = AsyncMock(return_value=make_project(status="active", config={}))
    mock_api.get_primary_repository = AsyncMock(
        return_value=make_repository(git_url="https://github.com/org/test-repo")
    )
    github = mock_github_cls.return_value
    github.get_repo_scoped_token = AsyncMock(return_value="ghs_scoped")
    github.get_token = AsyncMock(return_value="ghs_installation_wide")

    await DeveloperNode().run(_state())

    github.get_repo_scoped_token.assert_awaited_once_with("org", "test-repo")
    github.get_token.assert_not_awaited()
    assert mock_spawn.await_args.kwargs["github_token"] == "ghs_scoped"  # noqa: S105
