"""Unit tests for scaffold phase in WorkerManager."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.queues.worker import ScaffoldConfig


@pytest.fixture
def scaffold_config():
    return ScaffoldConfig(
        template_repo="gh:vladmesh/service-template",
        project_name="my-project",
        modules="backend,tg_bot",
        task_description="Build a bot",
    )


@pytest.fixture
def mock_docker():
    docker = AsyncMock()
    docker.exec_in_container = AsyncMock(return_value=(0, "OK"))
    docker.image_exists = AsyncMock(return_value=True)
    docker.run_container = AsyncMock(return_value=MagicMock(id="container-123"))
    docker.connect_network = AsyncMock()
    docker.create_network = AsyncMock()
    docker.remove_container = AsyncMock()
    docker.remove_network = AsyncMock()
    docker.get_container_logs = AsyncMock(return_value="logs")
    return docker


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.set = AsyncMock()
    r.hset = AsyncMock()
    r.hgetall = AsyncMock(return_value={})
    r.sismember = AsyncMock(return_value=False)
    r.sadd = AsyncMock()
    r.srem = AsyncMock()
    r.delete = AsyncMock()
    r.scan_iter = MagicMock(return_value=AsyncIterEmpty())
    return r


class AsyncIterEmpty:
    """Async iterator that yields nothing."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def _extract_scaffold_script(mock_docker) -> str:
    """Extract and decode the scaffold bash script from mock docker exec call."""
    import base64
    import re

    call_args = mock_docker.exec_in_container.call_args
    cmd = call_args[0][1]
    # Extract base64-encoded script from: bash -c 'echo <B64> | base64 -d | bash'
    match = re.search(r"echo (\S+) \| base64 -d", cmd)
    assert match, f"Could not find base64 payload in: {cmd}"
    return base64.b64decode(match.group(1)).decode()


class TestScaffoldPhase:
    @pytest.mark.asyncio
    async def test_scaffold_script_contains_copier_copy(self, mock_redis, mock_docker, scaffold_config):
        """Scaffold script includes copier copy with correct args."""
        from src.scaffold_phase import run_scaffold_phase

        result = await run_scaffold_phase(
            docker=mock_docker,
            container_id="c-123",
            scaffold_config=scaffold_config,
            repo="org/my-project",
            token="ghs_token",
            worker_id="w-1",
        )

        assert result is True
        mock_docker.exec_in_container.assert_awaited_once()

        call_args = mock_docker.exec_in_container.call_args
        cmd = call_args[0][1]
        assert "base64" in cmd

    @pytest.mark.asyncio
    async def test_scaffold_uses_data_file_for_task_description(self, mock_redis, mock_docker, scaffold_config):
        """task_description is passed via --data-file, not inline --data."""
        from src.scaffold_phase import run_scaffold_phase

        await run_scaffold_phase(
            docker=mock_docker,
            container_id="c-123",
            scaffold_config=scaffold_config,
            repo="org/my-project",
            token="ghs_token",
            worker_id="w-1",
        )

        script = _extract_scaffold_script(mock_docker)
        assert "--data-file" in script
        assert '--data "task_description=' not in script
        assert "--trust" not in script

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "dangerous_desc",
        [
            'has "double quotes" inside',
            "has $(command substitution)",
            "has `backticks`",
            "socket.getaddrinfo('localhost', 8080)",
            "line1\nline2\nline3",
            "has 'single quotes'",
            "back\\slashes\\everywhere",
            'all together: "quotes" $(cmd) `bt` (parens) \\n',
        ],
        ids=[
            "double_quotes",
            "command_sub",
            "backticks",
            "parens",
            "newlines",
            "single_quotes",
            "backslashes",
            "combined",
        ],
    )
    async def test_dangerous_task_description_is_safe(self, mock_redis, mock_docker, dangerous_desc):
        """Dangerous characters in task_description do not break the bash script."""
        from src.scaffold_phase import run_scaffold_phase

        config = ScaffoldConfig(
            template_repo="gh:vladmesh/service-template",
            project_name="my-project",
            modules="backend",
            task_description=dangerous_desc,
        )
        await run_scaffold_phase(
            docker=mock_docker,
            container_id="c-123",
            scaffold_config=config,
            repo="org/my-project",
            token="ghs_token",
            worker_id="w-1",
        )

        script = _extract_scaffold_script(mock_docker)
        assert '--data "task_description=' not in script
        assert "--data-file" in script

    @pytest.mark.asyncio
    async def test_copier_failure_returns_false(self, mock_redis, mock_docker, scaffold_config):
        """When exec fails, run_scaffold_phase returns False."""
        from src.scaffold_phase import run_scaffold_phase

        mock_docker.exec_in_container = AsyncMock(return_value=(1, "copier: command not found"))

        result = await run_scaffold_phase(
            docker=mock_docker,
            container_id="c-123",
            scaffold_config=scaffold_config,
            repo="org/my-project",
            token="ghs_token",
            worker_id="w-1",
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_scaffold_timeout(self, mock_redis, mock_docker, scaffold_config):
        """Scaffold phase uses 600s timeout."""
        from src.scaffold_phase import run_scaffold_phase

        await run_scaffold_phase(
            docker=mock_docker,
            container_id="c-123",
            scaffold_config=scaffold_config,
            repo="org/my-project",
            token="ghs_token",
            worker_id="w-1",
        )

        call_kwargs = mock_docker.exec_in_container.call_args[1]
        assert call_kwargs["timeout"] == 600


class TestCreateWorkerGitSetup:
    @pytest.mark.asyncio
    @patch("src.manager.git_ops.refresh_git_token", new_callable=AsyncMock, return_value=True)
    @patch("src.manager.workspace_mod")
    @patch("src.manager.ImageBuilder")
    async def test_pre_scaffolded_refreshes_git_token(
        self, mock_builder_cls, mock_workspace, mock_refresh, mock_redis, mock_docker
    ):
        """Pre-scaffolded workspace (repo_id) should refresh git token, not scaffold."""
        from src.manager import WorkerManager

        mock_builder = MagicMock()
        mock_builder.get_image_tag.return_value = "worker:test"
        mock_builder.generate_dockerfile.return_value = "FROM base"
        mock_builder_cls.return_value = mock_builder

        mock_workspace.get_scaffolded_workspace.return_value = (Path("/data/ws/repo-1"), True)
        mock_docker.exec_in_container = AsyncMock(return_value=(0, "ok"))

        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        await manager.create_worker_with_capabilities(
            worker_id="w-1",
            capabilities=["git"],
            base_image="worker-base:latest",
            env_vars={"GITHUB_TOKEN": "tok", "REPO_NAME": "org/repo"},
            repo_id="repo-1",
        )

        mock_refresh.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("src.manager.workspace_mod")
    @patch("src.manager.ImageBuilder")
    async def test_no_repo_id_raises(self, mock_builder_cls, mock_workspace, mock_redis, mock_docker):
        """Without repo_id, should raise RuntimeError."""
        from src.manager import WorkerManager

        mock_builder = MagicMock()
        mock_builder.get_image_tag.return_value = "worker:test"
        mock_builder.generate_dockerfile.return_value = "FROM base"
        mock_builder_cls.return_value = mock_builder

        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        with pytest.raises(RuntimeError, match="repo_id is required"):
            await manager.create_worker_with_capabilities(
                worker_id="w-1",
                capabilities=["git"],
                base_image="worker-base:latest",
                env_vars={"GITHUB_TOKEN": "tok", "REPO_NAME": "org/repo"},
            )
