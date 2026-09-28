"""Docker exec environment and developer credential lifetime in a real container."""

import io
import os
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from shared.contracts.vocab import AgentType
from src import git_ops
from src.container_config import WorkerContainerConfig
from src.docker_ops import DockerClientWrapper
from src.manager import WorkerManager

BASE_IMAGE = os.environ.get(
    "GIT_CREDENTIAL_TEST_IMAGE", "codegen-orchestrator/worker-manager-test-runner:test"
)


@pytest.mark.asyncio
async def test_native_docker_refresh_and_later_developer_git_use_current_credential():
    docker = DockerClientWrapper()
    image = f"codegen-git-credential-test:{uuid4().hex}"
    definition = f"""FROM {BASE_IMAGE}
USER root
RUN useradd -m -u 1000 worker && mkdir /workspace && chown worker:worker /workspace
USER worker
WORKDIR /workspace
ENTRYPOINT ["sleep", "infinity"]
"""
    await docker._run(
        docker._client.images.build, fileobj=io.BytesIO(definition.encode()), tag=image, rm=True
    )
    container = None
    try:
        manager = WorkerManager(redis=AsyncMock(), docker_client=docker)
        # The broker is external to the Git boundary; no live registration here.
        manager._register_broker_worker = AsyncMock()
        environment = await manager._prepare_worker_env(
            WorkerContainerConfig(
                worker_id="git-service",
                worker_type="developer",
                agent_type=AgentType.CLAUDE,
                capabilities=["git"],
            ),
            {"GITHUB_TOKEN": "docker-current-harmless-token", "REPO_NAME": "org/repo"},
            None,
        )
        container = await docker.run_container(
            image,
            detach=True,
            network_mode="none",
            environment=environment,
        )
        # This is the exact origin format written by the released manager.
        setup = (
            "git init && git config user.name Fixture && "
            "git config user.email fixture@example.test && "
            "git remote add origin "
            "https://x-access-token:released-harmless-token@github.com/org/repo && "
            "git config core.hooksPath .githooks"
        )
        assert (await docker.exec_in_container(container.id, ["bash", "-c", setup]))[0] == 0
        for token in ("docker-current-harmless-token", "docker-refreshed-harmless-token"):
            assert await git_ops.refresh_git_token(
                docker, container.id, "org/repo", token, "test-worker"
            )
            exit_code, config = await docker.exec_in_container(
                container.id, ["cat", "/workspace/.git/config"]
            )
            assert exit_code == 0
            assert b"https://github.com/org/repo.git" in config
            assert b"released-harmless-token" not in config
            assert token.encode() not in config
            assert b"extraheader" not in config and b"helper" not in config
            assert b"hooksPath = .githooks" in config
            # No exec environment here: the developer inherits container config
            # and reads the newly refreshed store, rather than a manager-only token.
            code, output = await docker.exec_in_container(
                container.id,
                [
                    "bash",
                    "-c",
                    "printf 'protocol=https\\nhost=github.com\\npath=org/repo.git\\n\\n' "
                    "| git credential fill",
                ],
            )
            assert code == 0
            assert f"password={token}".encode() in output
            code, mode = await docker.exec_in_container(
                container.id, ["stat", "-c", "%a", git_ops.GIT_CREDENTIAL_PATH]
            )
            assert code == 0 and mode.strip() == b"600"
            code, _ = await docker.exec_in_container(
                container.id,
                ["bash", "-c", 'test "$GH_TOKEN" = "$GITHUB_TOKEN" && test -n "$GH_TOKEN"'],
            )
            assert code == 0
    finally:
        if container is not None:
            await docker.remove_container(container.id, force=True)
        await docker.remove_image(image, force=True)
