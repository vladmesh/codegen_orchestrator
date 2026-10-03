"""Released work survives while native Git reacquires auth at every operation."""

import base64
import os
from pathlib import Path
import subprocess
import sys
import traceback
from unittest.mock import AsyncMock, MagicMock

from fakeredis import aioredis
import pytest
from test_infra_git_no_product_hooks import _OWNERSHIP, _docker_mock, _git, _make_product_repo
from worker_wrapper.config import WorkerWrapperConfig
from worker_wrapper.wrapper import WorkerWrapper, build_agent_subprocess_env

from shared.contracts.dto.engineering_execution import EngineeringExecutionPhase
from shared.contracts.dto.worker import WorkerStatus
from shared.contracts.queues.worker_result import WorkerCompletedResult
from shared.tests.git_http_fixture import GitHTTPFixture
from shared.tests.worker_credential_fixture import WorkerCredentialFixture
from src import git_ops
from src.manager import WorkerManager

OLD_TOKEN = "synthetic-released-expired-token"  # noqa: S105 - harmless canary


@pytest.mark.parametrize("attribute", ["capability", "wwwauth", "state", "future"])
def test_native_helper_accepts_multivalued_attributes_and_reacquires(
    attribute, tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    with WorkerCredentialFixture(tmp_path) as broker:
        environment = {**os.environ, **broker.environment}
        for token in ("synthetic-first", "synthetic-after-expiry"):
            broker.token = token
            result = subprocess.run(
                [str(broker.helper), "get"],
                input=(
                    f"{attribute}[]=authtype\n{attribute}[]=state\n"
                    "protocol=https\nhost=github.com\npath=org/repo.git\n\n"
                ),
                env=environment,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout == f"username=x-access-token\npassword={token}\n\n"
            assert token not in result.stderr
        assert broker.requests == [{"repository": "org/repo"}] * 2
    assert not (tmp_path / ".git-credentials").exists()
    assert not (tmp_path / ".config/codegen/git-credentials").exists()


@pytest.mark.parametrize("attribute", ["protocol", "host", "path"])
def test_native_helper_rejects_ambiguous_repository_before_mint(attribute, tmp_path):
    with WorkerCredentialFixture(tmp_path) as broker:
        result = subprocess.run(
            [str(broker.helper), "get"],
            input=(
                "capability[]=authtype\ncapability[]=state\n"
                "protocol=https\nhost=github.com\npath=org/repo.git\n"
                f"{attribute}=other\n\n"
            ),
            env={**os.environ, **broker.environment},
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode != 0
        assert result.stdout == ""
        assert broker.requests == []
        assert broker.token not in result.stderr


class LocalDocker:
    def __init__(
        self, workspace, home, helper="/usr/local/bin/git-credential-codegen", environment=None
    ):
        self.workspace, self.home, self.helper = workspace, home, helper
        self.environment = environment or {}
        self.commands = []

    async def exec_in_container(self, _container_id, command, **_kwargs):
        self.commands.append(command)
        if isinstance(command, str):
            payload = command.split("echo ", 1)[1].split(" |", 1)[0]
            command = ["bash", "-c", base64.b64decode(payload).decode()]
        args = [
            arg.replace("cd /workspace", f"cd {self.workspace}").replace(
                "/usr/local/bin/git-credential-codegen", str(self.helper)
            )
            for arg in command
        ]
        result = subprocess.run(
            args,
            env={**os.environ, **self.environment, "HOME": str(self.home)},
            capture_output=True,
            # A manager checkout is a whole script with multiple Git/helper
            # processes. Give the fixture headroom when broad suites contend;
            # individual wrapper Git/auth command timeouts remain unchanged.
            timeout=120,
        )
        return result.returncode, result.stdout + result.stderr

    async def exec_capture(self, *args, **kwargs):
        code, output = await self.exec_in_container(*args, **kwargs)
        return code, output, b""


@pytest.mark.asyncio
async def test_reused_worker_and_long_turn_publish_after_expiry(tmp_path, monkeypatch):
    with (
        WorkerCredentialFixture(tmp_path) as broker,
        GitHTTPFixture(tmp_path, broker.token, github_proxy=True) as remote,
    ):
        workspace = tmp_path / "workspace"
        remote.run("clone", str(remote.remote), str(workspace))
        remote.run("-C", str(workspace), "config", "user.name", "Developer")
        remote.run("-C", str(workspace), "config", "user.email", "developer@example.test")
        remote.run(
            "-C",
            str(workspace),
            "remote",
            "set-url",
            "origin",
            f"https://x-access-token:{OLD_TOKEN}@github.com/org/repo",
        )
        remote.run("-C", str(workspace), "checkout", "-b", "story/resume")
        (workspace / "unpublished.txt").write_text("preserved work")
        remote.run("-C", str(workspace), "add", ".")
        remote.run("-C", str(workspace), "commit", "-m", "preserved")
        preserved = remote.run("-C", str(workspace), "rev-parse", "HEAD")
        home = tmp_path / "home"
        home.mkdir()
        (home / ".gitconfig").write_bytes(remote.config_path.read_bytes())
        artifact = home / ".config/codegen/git-credentials"
        artifact.parent.mkdir(parents=True)
        artifact.write_text(OLD_TOKEN)
        remote.run("-C", str(workspace), "config", "credential.helper", "store")
        remote.run(
            "-C",
            str(workspace),
            "config",
            "http.https://github.com/.extraheader",
            "Authorization: stale",
        )
        environment = {**broker.environment, "PATH": remote.environment["PATH"]}
        monkeypatch.setenv("PATH", environment["PATH"])
        monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
        docker = LocalDocker(workspace, home, broker.helper, environment)
        assert await git_ops.configure_git_credentials(docker, "worker", "org/repo", "fixture")
        assert not artifact.exists()
        assert remote.run("-C", str(workspace), "rev-parse", "HEAD") == preserved
        assert await git_ops.checkout_branch(docker, "worker", "story/resume", "fixture")
        monkeypatch.setattr("worker_wrapper.wrapper.WORKSPACE_DIR", str(workspace))
        monkeypatch.setenv("HOME", str(home))
        for key, value in broker.environment.items():
            monkeypatch.setenv(key, value)
        wrapper = WorkerWrapper(
            WorkerWrapperConfig(
                worker_id="fixture",
                broker_url=broker.environment["WORKER_BROKER_URL"],
                broker_token="synthetic-broker-credential-32-characters",  # noqa: S106
                agent_type="noop",
            ),
            broker_client=AsyncMock(),
        )
        for index in range(2):
            # Same worker and config; the original creation token has expired.
            broker.token = remote.token = f"synthetic-reused-{index}"
            assert wrapper._git_auth_preflight()
            await wrapper._git_pull()
            agent_env = build_agent_subprocess_env(
                {**os.environ, "GITHUB_TOKEN": OLD_TOKEN, "GH_TOKEN": OLD_TOKEN}
            )
            assert "GH_TOKEN" not in agent_env and "GITHUB_TOKEN" not in agent_env
            pulled = subprocess.run(
                ["git", "pull", "--ff-only", "origin", "story/resume"],
                cwd=workspace,
                env=agent_env,
                capture_output=True,
                timeout=15,
            )
            assert pulled.returncode == 0, pulled.stderr
            (workspace / f"turn-{index}.txt").write_text("new work")
            remote.run("-C", str(workspace), "add", ".")
            remote.run("-C", str(workspace), "commit", "-m", "turn")
            head = remote.run("-C", str(workspace), "rev-parse", "HEAD")
            # Simulate >60 minutes during one turn; invalidate its starting token.
            broker.token = remote.token = f"synthetic-publication-{index}"
            count = len(broker.requests)
            result, error = wrapper._pushed_completed_result(
                WorkerCompletedResult(commit_sha=head, content="done"), "story/resume"
            )
            assert error is None and result.commit_sha == head
            assert remote.run("-C", str(remote.remote), "rev-parse", "story/resume") == head
            assert len(broker.requests) >= count + 2  # push + exact SHA readback
            exposed = (
                (workspace / ".git/config").read_text()
                + (home / ".gitconfig").read_text()
                + str(docker.commands)
                + str(remote.argv())
            )
            assert OLD_TOKEN not in exposed and broker.token not in exposed
        denied = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\npath=other/repo.git\n\n",
            text=True,
            capture_output=True,
            env=agent_env,
            cwd=workspace,
            timeout=15,
        )
        assert denied.returncode != 0 and broker.token not in denied.stdout + denied.stderr
        for operation in ("approve", "reject"):
            subprocess.run(
                ["git", "credential", operation],
                input=f"protocol=https\nhost=github.com\npath=org/repo.git\npassword={broker.token}\n\n",
                text=True,
                capture_output=True,
                env=agent_env,
                cwd=workspace,
                check=True,
                timeout=15,
            )
        assert not artifact.exists() and not (home / ".git-credentials").exists()
        broker.refused = True
        wrapper.execute_agent = AsyncMock()
        wrapper._prepare_workspace = AsyncMock()
        await wrapper._run_turn("lease", {"request_id": "next"})
        wrapper.execute_agent.assert_not_awaited()
        wrapper._prepare_workspace.assert_not_awaited()
        refusal = wrapper.broker.submit_output.await_args.args[1]
        assert refusal.execution.execution_phase is EngineeringExecutionPhase.PRE_AGENT_REFUSED
        assert refusal.cost_usd is None and refusal.claude_evidence is None


@pytest.mark.asyncio
async def test_failed_sanitization_refuses_checkout(monkeypatch):
    checkout = AsyncMock()
    monkeypatch.setattr(git_ops, "configure_git_credentials", AsyncMock(return_value=False))
    monkeypatch.setattr(git_ops, "checkout_branch", checkout)
    manager = WorkerManager(redis=AsyncMock(), docker_client=MagicMock())
    with pytest.raises(RuntimeError, match="credential"):
        await manager._prepare_worker_checkout(
            "worker", "id", "repo-id", {"REPO_NAME": "org/repo"}, "story/resume"
        )
    checkout.assert_not_awaited()


@pytest.mark.asyncio
async def test_sanitization_failure_logs_no_released_token(capsys):
    docker = MagicMock()
    docker.exec_in_container = AsyncMock(side_effect=RuntimeError(OLD_TOKEN))
    assert not await git_ops.configure_git_credentials(docker, "worker", "org/repo", "id")
    logs = capsys.readouterr()
    assert OLD_TOKEN not in logs.out + logs.err


@pytest.mark.asyncio
async def test_scaffolder_then_manager_then_ensure_keep_workspace_auth_clean(tmp_path, monkeypatch):
    root = Path(__file__).parents[4]
    code = """import asyncio, os
from types import SimpleNamespace
from src.scaffold import run_ensure_workspace
result = asyncio.run(run_ensure_workspace(
    repository_id="repo-id", project_name="fixture", repo_full_name="org/repo",
    github_token=os.environ["GITHUB_TOKEN"], repo_exists_on_github=True,
    settings=SimpleNamespace(workspace_base_path=os.environ["FIXTURE_WORKSPACE_ROOT"]),
))
assert result.success, result.error
"""
    workspaces = tmp_path / "workspaces"
    with GitHTTPFixture(tmp_path, OLD_TOKEN) as remote:
        environment = {
            **os.environ,
            **remote.environment,
            "GITHUB_TOKEN": OLD_TOKEN,
            "FIXTURE_WORKSPACE_ROOT": str(workspaces),
            "PYTHONPATH": f"{root / 'services' / 'scaffolder'}:{root}",
        }
        result = subprocess.run(
            [sys.executable, "-P", "-c", code],
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr + result.stdout
        workspace = workspaces / "repo-id"
        home = tmp_path / "worker-home"
        home.mkdir()
        assert await git_ops.configure_git_credentials(
            LocalDocker(workspace, home), "worker", "org/repo", "worker-id"
        )
        config = (workspace / ".git" / "config").read_text()
        assert "https://github.com/org/repo.git" in config
        assert OLD_TOKEN not in config
        result = subprocess.run(
            [sys.executable, "-P", "-c", code],
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert (workspace / ".git" / "config").read_text() == config


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_boundary",
    ["origin_upgrade", "credential_remove", "global_config_read", "global_config_write"],
)
async def test_failed_sanitization_never_publishes_ready_or_injects_agent_materials(
    failure_boundary, monkeypatch, tmp_path
):
    redis = aioredis.FakeRedis(decode_responses=True)
    docker = _docker_mock()
    _, workspace = _make_product_repo(tmp_path)
    _git(
        workspace,
        "remote",
        "set-url",
        "origin",
        f"https://x-access-token:{OLD_TOKEN}@github.com/org/repo",
    )
    home = tmp_path / "worker-home"
    home.mkdir()
    credential_path = home / ".config/codegen/git-credentials"
    if failure_boundary == "origin_upgrade":
        (workspace / ".git" / "config.lock").write_text("another Git writer holds the lock")
    elif failure_boundary == "credential_remove":
        credential_path.mkdir(parents=True)
    elif failure_boundary == "global_config_read":
        (home / ".gitconfig").mkdir()
    # procfs cannot create a global configuration tempfile, even as root.
    # No credential artifact is written by setup.
    writer_home = Path("/proc/self") if failure_boundary == "global_config_write" else home
    docker.exec_in_container = LocalDocker(workspace, writer_home).exec_in_container
    manager = WorkerManager(redis=redis, docker_client=docker)
    monkeypatch.setattr(manager, "ensure_or_build_image", AsyncMock(return_value="worker:test"))
    monkeypatch.setattr("src.manager.settings.ENVIRONMENT", "test")
    monkeypatch.setattr("src.manager.settings.SCAFFOLDED_WORKSPACE_PATH", str(tmp_path))
    monkeypatch.setattr(
        "src.manager.workspace_mod.get_scaffolded_workspace", lambda *_: (workspace, True)
    )
    inject = AsyncMock()
    monkeypatch.setattr(manager, "_inject_worker_materials", inject)
    statuses = []
    original_hset = redis.hset

    async def hset(name, *args, **kwargs):
        if name == "worker:status:unsafe":
            statuses.append(kwargs.get("mapping", {}).get("status"))
        return await original_hset(name, *args, **kwargs)

    monkeypatch.setattr(redis, "hset", hset)
    with pytest.raises(RuntimeError, match="credential"):
        await manager.create_worker_with_capabilities(
            worker_id="unsafe",
            capabilities=["git"],
            base_image="worker:test",
            ownership=_OWNERSHIP,
            repo_id="repo-id",
            instructions="agent materials",
            env_vars={"REPO_NAME": "org/repo", "GITHUB_TOKEN": OLD_TOKEN},
        )
    inject.assert_not_awaited()
    assert WorkerStatus.RUNNING not in statuses
    assert "credential" in await redis.get("worker:error:unsafe")
    if failure_boundary == "credential_remove":
        assert credential_path.is_dir()
    else:
        assert not credential_path.exists()
    assert _git(workspace, "rev-parse", "HEAD")


@pytest.mark.asyncio
async def test_setup_writer_cannot_import_product_shadow_modules(tmp_path, monkeypatch):
    _, workspace = _make_product_repo(tmp_path)
    for module in ("pathlib", "tempfile"):
        (workspace / f"{module}.py").write_text('raise RuntimeError("product module imported")\n')
    home = tmp_path / "worker-home"
    home.mkdir()
    credential_path = home / ".config" / "codegen" / "git-credentials"
    assert await git_ops.configure_git_credentials(
        LocalDocker(workspace, home), "worker", "org/repo", "worker-id"
    )
    assert not credential_path.exists()


@pytest.mark.asyncio
async def test_native_container_creation_error_cannot_echo_github_environment(capsys):
    redis = aioredis.FakeRedis(decode_responses=True)
    docker = _docker_mock()
    encoded = base64.b64encode(f"x-access-token:{OLD_TOKEN}".encode()).decode()
    docker.run_container.side_effect = RuntimeError(
        f"Docker refused environment: {OLD_TOKEN} {encoded}"
    )
    manager = WorkerManager(redis=redis, docker_client=docker)
    with pytest.raises(RuntimeError) as failure:
        await manager.create_worker(
            "worker-id",
            "worker:test",
            ownership=_OWNERSHIP,
            env_vars={"GITHUB_TOKEN": OLD_TOKEN, "GH_TOKEN": OLD_TOKEN},
            publish_ready=False,
        )
    captured = capsys.readouterr()
    observable = captured.out + captured.err + "".join(traceback.format_exception(failure.value))
    observable += await redis.get("worker:error:worker-id")
    assert "Docker refused environment" in observable
    assert OLD_TOKEN not in observable and encoded not in observable
