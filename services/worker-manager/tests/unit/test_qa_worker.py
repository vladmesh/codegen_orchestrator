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
from src import qa_egress
from src import workspace as workspace_mod
from src.manager import QA_WORKER_TYPE, WorkerManager


QA_NETWORK = "codegen_qa_egress"


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
    wrapper.inspect_network = AsyncMock(return_value={"Internal": True})
    wrapper.inspect_container = AsyncMock(return_value={"NetworkSettings": {"Networks": {QA_NETWORK: {}}}})
    wrapper.exec_in_container = AsyncMock(return_value=(0, "ok"))
    wrapper.get_container_logs = AsyncMock(return_value="")
    return wrapper


def _executor_run(wrapper):
    """The container that runs the agent, not the run's egress proxy."""
    [call] = [
        call
        for call in wrapper.run_container.await_args_list
        if not call.kwargs["name"].startswith(qa_egress.PROXY_NAME_PREFIX)
    ]
    return call.kwargs


def _proxy_run(wrapper):
    """The run's egress proxy, the one container allowed a second network leg."""
    [call] = [
        call
        for call in wrapper.run_container.await_args_list
        if call.kwargs["name"].startswith(qa_egress.PROXY_NAME_PREFIX)
    ]
    return call.kwargs


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
            settings.QA_EGRESS_NETWORK = QA_NETWORK
            settings.QA_CLAUDE_BACKEND_HOSTS = ""
            settings.QA_CODEX_BACKEND_HOSTS = ""
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

        mounted = _executor_run(wrapper)["volumes"]
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

        env = _executor_run(wrapper)["environment"]
        assert "GITHUB_TOKEN" not in env
        assert "GH_TOKEN" not in env
        assert env["QA_CAPABILITY_TOKEN"] == "run-token"

    async def test_it_gets_no_project_network_of_its_own(self, qa_worker):
        wrapper, _, _ = await qa_worker()

        wrapper.create_network.assert_not_awaited()
        assert _executor_run(wrapper)["network"] == QA_NETWORK

    async def test_it_keeps_the_hardening_every_worker_has(self, qa_worker):
        wrapper, _, _ = await qa_worker()

        run_kwargs = _executor_run(wrapper)
        assert run_kwargs["cap_drop"] == ["ALL"]
        assert run_kwargs["security_opt"] == ["no-new-privileges:true"]


class TestItCannotReachTheApplicationAtAll:
    """The guarantee is the network, and these are the ways it is held.

    A CLI agent has a shell, so "QA does not write to the application" cannot
    rest on the tool set any more. It rests on the executor being attached to
    one internal network and nothing else, with one CONNECT-only proxy opening
    the assigned CLI's model backend. Every check here fails the run closed.
    """

    async def test_it_is_attached_to_the_internal_qa_network_and_nothing_else(self, qa_worker):
        wrapper, _, _ = await qa_worker()

        assert _executor_run(wrapper)["network"] == QA_NETWORK
        wrapper.inspect_network.assert_awaited_with(QA_NETWORK)

    async def test_a_network_that_can_route_off_itself_stops_the_run(self, qa_worker):
        """Without `internal: true` the container would simply have the internet."""
        wrapper = _docker_mock()
        wrapper.inspect_network = AsyncMock(return_value={"Internal": False})

        with pytest.raises(qa_egress.QAEgressError, match="not internal"):
            await qa_worker(docker=wrapper)

        assert not [
            call
            for call in wrapper.run_container.await_args_list
            if not call.kwargs["name"].startswith(qa_egress.PROXY_NAME_PREFIX)
        ], "an executor container was started before its egress policy held"

    async def test_a_missing_qa_network_stops_the_run(self, qa_worker):
        wrapper = _docker_mock()
        wrapper.inspect_network = AsyncMock(side_effect=RuntimeError("network not found"))

        with pytest.raises(qa_egress.QAEgressError, match="could not be inspected"):
            await qa_worker(docker=wrapper)

    async def test_a_container_that_ended_up_on_a_second_network_stops_the_run(self, qa_worker):
        """Asked-for and attached are different facts; only the second one counts."""
        wrapper = _docker_mock()
        wrapper.inspect_container = AsyncMock(
            return_value={"NetworkSettings": {"Networks": {QA_NETWORK: {}, "codegen_worker": {}}}}
        )

        with pytest.raises(qa_egress.QAEgressError, match="codegen_worker"):
            await qa_worker(docker=wrapper)

    async def test_the_run_opens_only_its_assigned_agents_model_backend(self, qa_worker):
        wrapper, _, _ = await qa_worker()

        proxy = _proxy_run(wrapper)
        assert proxy["command"] == list(qa_egress.DEFAULT_MODEL_BACKENDS[AgentType.CLAUDE])
        assert proxy["network"] == QA_NETWORK
        # The proxy — and only the proxy — gets the second leg that has a route out.
        wrapper.connect_network.assert_awaited_once_with("codegen_worker", "container-id")

    async def test_a_codex_run_opens_codex_backends_and_not_claudes(self, qa_worker):
        with patch("src.codex_auth.validate_codex_host_session"):
            wrapper, _, _ = await qa_worker(agent_type=AgentType.CODEX)

        assert _proxy_run(wrapper)["command"] == list(qa_egress.DEFAULT_MODEL_BACKENDS[AgentType.CODEX])

    async def test_the_executor_is_pointed_at_the_proxy_for_everything_else(self, qa_worker):
        wrapper, _, _ = await qa_worker()

        env = _executor_run(wrapper)["environment"]
        assert env["HTTPS_PROXY"] == f"http://{qa_egress.proxy_container_name('qa-1')}:3128"
        assert env["https_proxy"] == env["HTTPS_PROXY"]
        assert "HTTP_PROXY" not in env
        # The two runtime services live on the executor's own network; sending
        # them through the proxy would only get them refused.
        assert set(env["NO_PROXY"].split(",")) == {
            "localhost",
            "127.0.0.1",
            "qa-worker",
            "worker-broker",
        }

    async def test_a_proxy_that_never_listens_stops_the_run(self, qa_worker, monkeypatch):
        monkeypatch.setattr(qa_egress, "PROXY_READY_ATTEMPTS", 2)
        monkeypatch.setattr(qa_egress, "PROXY_READY_DELAY", 0)
        wrapper = _docker_mock()
        wrapper.exec_in_container = AsyncMock(return_value=(1, "connection refused"))

        with pytest.raises(qa_egress.QAEgressError, match="never accepted a connection"):
            await qa_worker(docker=wrapper)

    async def test_a_failed_start_takes_the_proxy_with_it(self, qa_worker):
        wrapper = _docker_mock()
        wrapper.inspect_container = AsyncMock(
            return_value={"NetworkSettings": {"Networks": {QA_NETWORK: {}, "bridge": {}}}}
        )

        with pytest.raises(qa_egress.QAEgressError):
            await qa_worker(docker=wrapper)

        removed = [call.args[0] for call in wrapper.remove_container.await_args_list]
        assert qa_egress.proxy_container_name("qa-1") in removed

    async def test_deleting_the_executor_takes_the_proxy_with_it(self, qa_worker, tmp_path):
        wrapper, manager, _ = await qa_worker()

        with patch("src.manager.settings") as settings:
            settings.WORKER_IMAGE_PREFIX = "worker"
            settings.SCAFFOLDED_WORKSPACE_PATH = str(tmp_path)
            await manager.delete_worker("qa-1", reason="completed")

        removed = [call.args[0] for call in wrapper.remove_container.await_args_list]
        assert qa_egress.proxy_container_name("qa-1") in removed

    async def test_a_developer_worker_keeps_the_ordinary_worker_network(self, tmp_path):
        """This is the QA worker's network, not a change to everybody's."""
        wrapper = _docker_mock()
        redis = aioredis.FakeRedis(decode_responses=True)
        manager = WorkerManager(redis=redis, docker_client=wrapper)
        repo = tmp_path / "repo-1"
        repo.mkdir()

        with (
            patch("src.manager.settings") as settings,
            patch.object(manager, "ensure_or_build_image", new_callable=AsyncMock, return_value="w:latest"),
            patch("src.manager.workspace_mod.prepare_worker_paths"),
            patch("src.manager.workspace_mod.get_scaffolded_workspace", return_value=(repo, True)),
        ):
            settings.ENVIRONMENT = "production"
            settings.DOCKER_NETWORK = ""
            settings.WORKER_NETWORK = "codegen_worker"
            settings.QA_EGRESS_NETWORK = QA_NETWORK
            settings.SCAFFOLDED_WORKSPACE_PATH = str(tmp_path)
            settings.WORKER_BROKER_URL = "http://worker-broker:8001"
            settings.WORKER_SUBPROCESS_TIMEOUT_SECONDS = 300
            settings.WORKER_IMAGE_PREFIX = "worker"
            settings.WORKER_DOCKER_LABELS = "{}"
            settings.WORKER_TRANSCRIPT_STORAGE_PATH = str(tmp_path / "transcripts")
            settings.WORKER_TRANSCRIPT_MAX_BYTES = 1024
            settings.WORKER_TRANSCRIPT_RETENTION_DAYS = 1
            await manager.create_worker_with_capabilities(
                worker_id="dev-1",
                capabilities=[],
                base_image="worker-base:latest",
                agent_type=AgentType.CLAUDE,
                repo_id="repo-1",
            )

        assert wrapper.run_container.await_args.kwargs["network"] == "codegen_worker"
        wrapper.inspect_network.assert_not_awaited()


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

        async def exec_in_container(container_id, cmd, **kwargs):
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

        async def exec_in_container(container_id, cmd, **kwargs):
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


class TestTheCredentialSaysWhatKindOfWorkerItIs:
    """Both boundaries authorize on the type, so creation has to record it.

    A worker's broker token is readable by the agent that runs under it, so the
    only thing standing between a QA executor and the management host's Docker
    daemon is what the server knows about the token's owner.
    """

    async def test_the_broker_credential_is_issued_as_a_qa_credential(self, qa_worker):
        await qa_worker()

        call = WorkerManager._register_broker_worker.await_args
        assert call.args[0] == "qa-1"
        assert call.args[2] == QA_WORKER_TYPE

    async def test_the_type_is_recorded_before_the_credential_exists(self, qa_worker):
        """Ordering, not just presence: an unrecorded type is refused everything.

        If the record were written after the token was handed out, a worker that
        used it in that window would be refused its own turn.
        """
        manager_holder: dict = {}
        recorded: dict[str, str | None] = {}

        async def capture(worker_id, token, worker_type):
            recorded["at_registration"] = await manager_holder["manager"].redis.hget(
                f"worker:meta:{worker_id}", "worker_type"
            )

        WorkerManager._register_broker_worker.side_effect = capture
        try:
            await qa_worker(manager_holder=manager_holder)
        finally:
            WorkerManager._register_broker_worker.side_effect = None

        assert recorded["at_registration"] == QA_WORKER_TYPE
