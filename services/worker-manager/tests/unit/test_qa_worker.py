"""The central QA executor as worker-manager builds it.

It is started by the same mechanism as a developer worker — the same
`create_worker_with_capabilities`, the same image, the same broker, the same
host session mount — and the differences are all in what it is *not* given: no
repository, no git credentials, nothing that survives the container. What it is
given instead is one command, which is its only route to the deployment.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fakeredis import aioredis
import pytest

from shared.contracts.dto.worker import WorkerStatus
from shared.contracts.queues.worker import AgentType, WorkerConfig
from shared.qa_probe_cli import QA_PROBE_PATH
from src import workspace as workspace_mod
from src.manager import QA_WORKER_TYPE, WorkerManager


def _docker_mock():
    wrapper = MagicMock()
    wrapper.image_exists = AsyncMock(return_value=True)
    wrapper.get_image_label = AsyncMock(return_value="basehash0001")
    wrapper.remove_container = AsyncMock()
    container = MagicMock()
    container.id = "container-id"
    wrapper.run_container = AsyncMock(return_value=container)
    wrapper.create_network = AsyncMock()
    wrapper.connect_network = AsyncMock()
    wrapper.remove_network = AsyncMock()
    wrapper.exec_in_container = AsyncMock(return_value=(0, "ok"))
    wrapper.get_container_logs = AsyncMock(return_value="")
    return wrapper


@pytest.fixture
def qa_worker(tmp_path):
    """Create one QA executor and hand back everything Docker was asked for."""

    async def _create(
        *,
        worker_id="qa-1",
        agent_type=AgentType.CLAUDE,
        docker=None,
        manager_holder=None,
        **overrides,
    ):
        wrapper = docker or _docker_mock()
        redis = aioredis.FakeRedis(decode_responses=True)
        manager = WorkerManager(redis=redis, docker_client=wrapper)
        if manager_holder is not None:
            # So a test can read the state a *failed* creation left behind.
            manager_holder["manager"] = manager
        with (
            patch("src.manager.settings") as settings,
            patch.object(manager, "ensure_or_build_image", new_callable=AsyncMock, return_value="w:latest"),
            patch("src.manager.workspace_mod.prepare_worker_paths"),
        ):
            settings.ENVIRONMENT = "production"
            settings.DOCKER_NETWORK = ""
            settings.WORKER_NETWORK = "codegen_worker"
            settings.SCAFFOLDED_WORKSPACE_PATH = str(tmp_path)
            settings.WORKER_BROKER_URL = "http://worker-broker:8001"
            settings.WORKER_SUBPROCESS_TIMEOUT_SECONDS = 300
            settings.WORKER_IMAGE_PREFIX = "worker"
            settings.WORKER_DOCKER_LABELS = "{}"
            settings.WORKER_TRANSCRIPT_STORAGE_PATH = str(tmp_path / "transcripts")
            settings.WORKER_TRANSCRIPT_MAX_BYTES = 1024
            settings.WORKER_TRANSCRIPT_RETENTION_DAYS = 1
            kwargs = {
                "worker_id": worker_id,
                "capabilities": [],
                "base_image": "worker-base:latest",
                "agent_type": agent_type,
                "worker_type": QA_WORKER_TYPE,
                "instructions": "# QA executor",
                "task_content": "run the regression test",
                "env_vars": {
                    "QA_CAPABILITY_URL": "http://qa-worker:41234/qa/call",
                    "QA_CAPABILITY_TOKEN": "run-token",
                },
            }
            kwargs.update(overrides)
            await manager.create_worker_with_capabilities(**kwargs)
        return wrapper, manager, redis

    return _create


class TestAQaExecutorNeedsNoRepository:
    async def test_it_is_created_without_a_repo_id(self, qa_worker, tmp_path):
        """A developer worker refuses without one; QA has nothing to check out."""
        wrapper, _, _ = await qa_worker()

        wrapper.run_container.assert_awaited_once()
        mounted = wrapper.run_container.await_args.kwargs["volumes"]
        [workspace] = [host for host, spec in mounted.items() if spec["bind"] == "/workspace"]
        assert Path(workspace).parent == tmp_path
        assert Path(workspace).name == f"{workspace_mod.QA_WORKSPACE_PREFIX}qa-1"

    async def test_its_workspace_is_empty(self, qa_worker, tmp_path):
        await qa_worker()

        workspace = tmp_path / f"{workspace_mod.QA_WORKSPACE_PREFIX}qa-1"
        assert workspace.is_dir()
        assert list(workspace.iterdir()) == []

    async def test_it_carries_no_github_credential(self, qa_worker):
        wrapper, _, _ = await qa_worker()

        env = wrapper.run_container.await_args.kwargs["environment"]
        assert "GITHUB_TOKEN" not in env
        assert "GH_TOKEN" not in env
        assert env["QA_CAPABILITY_TOKEN"] == "run-token"

    async def test_it_gets_no_project_network_of_its_own(self, qa_worker):
        wrapper, _, _ = await qa_worker()

        wrapper.create_network.assert_not_awaited()
        assert wrapper.run_container.await_args.kwargs["network"] == "codegen_worker"

    async def test_it_keeps_the_hardening_every_worker_has(self, qa_worker):
        wrapper, _, _ = await qa_worker()

        run_kwargs = wrapper.run_container.await_args.kwargs
        assert run_kwargs["cap_drop"] == ["ALL"]
        assert run_kwargs["security_opt"] == ["no-new-privileges:true"]


class TestTheOneCommandItIsGiven:
    async def test_the_capability_command_is_installed_executable(self, qa_worker):
        wrapper, _, _ = await qa_worker()

        installed = [
            call.args[1] for call in wrapper.exec_in_container.await_args_list if QA_PROBE_PATH in call.args[1]
        ]
        assert installed, "the QA executor was started without its one command"
        assert "chmod" in installed[0]

    async def test_a_failure_to_install_it_fails_the_worker(self, qa_worker):
        """An executor without it would go looking for another way to reach the app."""
        wrapper = _docker_mock()

        async def exec_in_container(container_id, cmd):
            if QA_PROBE_PATH in cmd:
                return 1, "read-only file system"
            return 0, "ok"

        wrapper.exec_in_container = AsyncMock(side_effect=exec_in_container)

        with pytest.raises(RuntimeError, match="QA capability command"):
            await qa_worker(docker=wrapper)

    async def test_a_half_built_executor_is_failed_and_still_owned(self, qa_worker, tmp_path):
        """The container is already RUNNING by then, so two things have to be true.

        The status must say failed — a client polling it would otherwise send a
        run into a container that has to improvise a route to the deployment —
        and the workspace must still be recognisable as scratch, so deleting the
        worker takes it with it.
        """
        wrapper = _docker_mock()

        async def exec_in_container(container_id, cmd):
            return (1, "read-only file system") if QA_PROBE_PATH in cmd else (0, "ok")

        wrapper.exec_in_container = AsyncMock(side_effect=exec_in_container)
        manager_holder = {}

        try:
            await qa_worker(docker=wrapper, manager_holder=manager_holder)
        except RuntimeError:
            pass

        manager = manager_holder["manager"]
        assert await manager.redis.hget("worker:meta:qa-1", "worker_type") == QA_WORKER_TYPE
        assert await manager.redis.hget("worker:status:qa-1", "status") == WorkerStatus.FAILED

        workspace = tmp_path / f"{workspace_mod.QA_WORKSPACE_PREFIX}qa-1"
        with patch("src.manager.settings") as settings:
            settings.WORKER_IMAGE_PREFIX = "worker"
            settings.SCAFFOLDED_WORKSPACE_PATH = str(tmp_path)
            await manager.delete_worker("qa-1", reason="failed")

        assert not workspace.exists()


class TestNothingSurvivesTheRun:
    async def test_deleting_the_executor_removes_its_workspace(self, qa_worker, tmp_path):
        wrapper, manager, _ = await qa_worker()
        workspace = tmp_path / f"{workspace_mod.QA_WORKSPACE_PREFIX}qa-1"
        (workspace / "scratch.md").write_text("notes")

        with patch("src.manager.settings") as settings:
            settings.WORKER_IMAGE_PREFIX = "worker"
            settings.SCAFFOLDED_WORKSPACE_PATH = str(tmp_path)
            await manager.delete_worker("qa-1", reason="completed")

        assert not workspace.exists()

    async def test_a_developer_workspace_is_still_preserved(self, tmp_path):
        """The QA branch must not start deleting the repositories workers share."""
        wrapper = _docker_mock()
        redis = aioredis.FakeRedis(decode_responses=True)
        manager = WorkerManager(redis=redis, docker_client=wrapper)
        repo_workspace = tmp_path / "repo-1"
        repo_workspace.mkdir()
        await redis.hset(
            "worker:meta:dev-1",
            mapping={"worker_type": "developer", "workspace_path": str(repo_workspace)},
        )

        with (
            patch("src.manager.settings") as settings,
            patch("src.manager.ComposeRunner") as compose,
        ):
            settings.WORKER_IMAGE_PREFIX = "worker"
            settings.SCAFFOLDED_WORKSPACE_PATH = str(tmp_path)
            compose.return_value.run = AsyncMock(return_value=(0, "", ""))
            await manager.delete_worker("dev-1", reason="completed")

        assert repo_workspace.exists()


class TestTheContract:
    def test_a_qa_worker_config_is_valid_without_a_repository(self):
        config = WorkerConfig(
            name="qa-1",
            worker_type="qa",
            agent_type=AgentType.CLAUDE,
            instructions="# QA executor",
            allowed_commands=["*"],
            capabilities=[],
        )

        assert config.repo_id is None
        assert config.worker_type == "qa"
