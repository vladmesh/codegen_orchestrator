"""Real persistent Git config and credential use across worker token refreshes."""

import base64
import os
from pathlib import Path
import shutil
import subprocess
import sys
import traceback
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fakeredis import aioredis
import pytest
from test_infra_git_no_product_hooks import _OWNERSHIP, _docker_mock, _git, _make_product_repo
from worker_wrapper.wrapper import build_agent_subprocess_env

from shared.contracts.dto.worker import WorkerStatus
from shared.tests.git_http_fixture import GitHTTPFixture
from src import git_ops
from src.manager import WorkerManager
from src.routers.workspaces import get_workspace_file

TOKEN = "worker-harmless-current-token-canary"  # noqa: S105 - harmless canary
OLD_TOKEN = "worker-harmless-released-token-canary"  # noqa: S105 - harmless canary


class LocalDocker:
    """Execute Docker's command locally; only the container paths are redirected."""

    def __init__(self, workspace, home):
        self.workspace = workspace
        self.home = home
        self.commands = []

    async def exec_in_container(self, container_id, command, **kwargs):
        self.commands.append(command)
        environment = {**os.environ, "HOME": str(self.home), **kwargs.get("environment", {})}
        if isinstance(command, str) and " | base64 -d | bash" in command:
            payload = command.split("echo ", 1)[1].split(" |", 1)[0]
            command = base64.b64decode(payload).decode()
        if isinstance(command, str):
            args = ["bash", "-c", command]
        else:
            args = command
        args = [arg.replace("cd /workspace", f"cd {self.workspace}") for arg in args]
        result = subprocess.run(args, env=environment, capture_output=True, timeout=30)
        return result.returncode, result.stdout + result.stderr

    async def exec_capture(self, container_id, command, **kwargs):
        code, output = await self.exec_in_container(container_id, command, **kwargs)
        return code, output, b""


@pytest.mark.asyncio
async def test_released_origin_is_upgraded_and_developer_uses_current_credential(
    tmp_path, monkeypatch
):
    _, workspace = _make_product_repo(tmp_path)
    _git(workspace, "checkout", "-b", "story/resume")
    (workspace / "unpublished.txt").write_text("keep this work")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "unpublished work")
    tip = _git(workspace, "rev-parse", "HEAD")
    _git(workspace, "config", "branch.story/resume.remote", "origin")
    _git(workspace, "config", "branch.story/resume.merge", "refs/heads/story/resume")
    _git(
        workspace,
        "remote",
        "set-url",
        "origin",
        f"https://x-access-token:{OLD_TOKEN}@github.com/org/repo",
    )
    home = tmp_path / "worker-home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        "[user]\n\tname = Keep existing identity\n"
        '[credential "https://other.example"]\n\thelper = other-helper\n'
        '[credential "https://github.com"]\n\thelper = stale-helper\n'
    )
    credential_path = home / ".config" / "codegen" / "git-credentials"
    monkeypatch.setattr(git_ops, "GIT_CREDENTIAL_PATH", str(credential_path), raising=False)
    docker = LocalDocker(workspace, home)
    for token in (TOKEN, "worker-harmless-refreshed-token-canary"):
        assert await git_ops.refresh_git_token(docker, "worker", "org/repo", token, "worker-id")
        config = (workspace / ".git" / "config").read_text()
        assert _git(workspace, "remote", "get-url", "origin") == "https://github.com/org/repo.git"
        assert _git(workspace, "rev-parse", "HEAD") == tip
        assert _git(workspace, "branch", "--show-current") == "story/resume"
        assert _git(workspace, "config", "branch.story/resume.merge") == "refs/heads/story/resume"
        assert _git(workspace, "config", "branch.story/resume.remote") == "origin"
        assert _git(workspace, "config", "core.hooksPath") == ".githooks"
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(scaffolded_workspace_path=tmp_path))
        )
        served = await get_workspace_file("workspace", ".git/config", request)
        assert served.content == config
        for secret in (OLD_TOKEN, TOKEN, token):
            assert secret not in config
            for representation in (
                secret,
                base64.b64encode(secret.encode()).decode(),
                base64.b64encode(f"x-access-token:{secret}".encode()).decode(),
            ):
                assert representation not in config + str(docker.commands)
        assert "extraheader" not in config.lower()
        assert "helper" not in config.lower()
        assert credential_path.stat().st_mode & 0o777 == 0o600
        assert credential_path.parent.stat().st_mode & 0o777 == 0o700
        # The shipped wrapper filters every GIT_* setting; HOME must suffice.
        agent_env = build_agent_subprocess_env({**os.environ, "HOME": str(home)})
        assert not any(key.startswith("GIT_") for key in agent_env)
        result = subprocess.run(
            ["git", "credential", "fill"],
            cwd=workspace,
            input="protocol=https\nhost=github.com\npath=org/repo.git\n\n",
            text=True,
            capture_output=True,
            env=agent_env,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert f"password={token}" in result.stdout
        assert OLD_TOKEN not in result.stdout
        global_config = (home / ".gitconfig").read_text()
        assert "Keep existing identity" in global_config
        assert "other-helper" in global_config and "stale-helper" not in global_config
        assert token not in global_config and OLD_TOKEN not in global_config
        assert "useHttpPath = true" in global_config
        assert (home / ".gitconfig").stat().st_mode & 0o777 == 0o600
    assert not list(credential_path.parent.glob("tmp*"))


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
    with GitHTTPFixture(tmp_path, TOKEN) as remote:
        environment = {
            **os.environ,
            **remote.environment,
            "GITHUB_TOKEN": TOKEN,
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
        monkeypatch.setattr(git_ops, "GIT_CREDENTIAL_PATH", str(home / "credentials"))
        assert await git_ops.refresh_git_token(
            LocalDocker(workspace, home), "worker", "org/repo", TOKEN, "worker-id"
        )
        config = (workspace / ".git" / "config").read_text()
        assert "https://github.com/org/repo.git" in config
        assert TOKEN not in config
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
@pytest.mark.parametrize("raises", [False, True])
async def test_refresh_failure_redacts_native_exec_output_and_exceptions(raises, capsys):
    encoded = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    failure = f"permission denied {TOKEN} Authorization: Basic {encoded}"
    docker = MagicMock()
    docker.exec_in_container = AsyncMock(
        side_effect=RuntimeError(failure) if raises else None,
        return_value=(1, failure.encode()),
    )
    assert not await git_ops.refresh_git_token(docker, "worker", "org/repo", TOKEN, "worker-id")
    logs = capsys.readouterr()
    assert TOKEN not in logs.out + logs.err
    assert encoded not in logs.out + logs.err
    assert "permission denied" in logs.out + logs.err
    assert TOKEN not in str(docker.exec_in_container.await_args.args)


@pytest.mark.asyncio
async def test_failed_refresh_refuses_checkout_and_instruction_exposure(monkeypatch):
    refresh = AsyncMock(return_value=False)
    checkout = AsyncMock()
    monkeypatch.setattr(git_ops, "refresh_git_token", refresh)
    monkeypatch.setattr(git_ops, "checkout_branch", checkout)
    manager = WorkerManager(redis=AsyncMock(), docker_client=MagicMock())
    with pytest.raises(RuntimeError, match="credential"):
        await manager._prepare_worker_checkout(
            "worker",
            "worker-id",
            "repo-id",
            {"REPO_NAME": "org/repo", "GITHUB_TOKEN": TOKEN},
            "story/resume",
        )
    checkout.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_with_repository_requires_credentials_before_checkout():
    manager = WorkerManager(redis=AsyncMock(), docker_client=MagicMock())
    with pytest.raises(RuntimeError, match="credentials"):
        await manager._prepare_worker_checkout("worker", "worker-id", "repo-id", {}, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_boundary",
    ["origin_upgrade", "credential_write", "global_config_read", "global_config_write"],
)
async def test_failed_refresh_never_publishes_ready_or_injects_agent_materials(
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
    credential_path = home / "credentials" / "git-credentials"
    monkeypatch.setattr(git_ops, "GIT_CREDENTIAL_PATH", str(credential_path))
    if failure_boundary == "origin_upgrade":
        (workspace / ".git" / "config.lock").write_text("another Git writer holds the lock")
    elif failure_boundary == "credential_write":
        credential_path.parent.write_text("a file obstructs credential storage")
    elif failure_boundary == "global_config_read":
        (home / ".gitconfig").mkdir()
    # procfs cannot create a global configuration tempfile, even as root.
    # Keep the credential path writable to reach the configuration write.
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
            env_vars={"REPO_NAME": "org/repo", "GITHUB_TOKEN": TOKEN},
        )
    inject.assert_not_awaited()
    assert WorkerStatus.RUNNING not in statuses
    assert "credential" in await redis.get("worker:error:unsafe")
    if failure_boundary != "global_config_write":
        assert not credential_path.exists()
    else:
        assert credential_path.stat().st_mode & 0o777 == 0o600
    if failure_boundary in ("origin_upgrade", "global_config_read"):
        assert OLD_TOKEN in (workspace / ".git" / "config").read_text()
    else:
        assert OLD_TOKEN not in (workspace / ".git" / "config").read_text()


@pytest.mark.asyncio
async def test_refresh_writer_cannot_import_product_shadow_modules(tmp_path, monkeypatch):
    _, workspace = _make_product_repo(tmp_path)
    for module in ("pathlib", "tempfile"):
        (workspace / f"{module}.py").write_text('raise RuntimeError("product module imported")\n')
    home = tmp_path / "worker-home"
    home.mkdir()
    credential_path = home / ".config" / "codegen" / "git-credentials"
    monkeypatch.setattr(git_ops, "GIT_CREDENTIAL_PATH", str(credential_path))
    assert await git_ops.refresh_git_token(
        LocalDocker(workspace, home), "worker", "org/repo", TOKEN, "worker-id"
    )
    assert TOKEN in credential_path.read_text()


@pytest.mark.asyncio
async def test_filtered_agent_fetch_and_push_use_private_home_config_after_refresh(
    tmp_path, monkeypatch
):
    with GitHTTPFixture(tmp_path, TOKEN, github_proxy=True) as remote:
        workspace = tmp_path / "workspace"
        remote.run("clone", str(remote.remote), str(workspace))
        remote.run("-C", str(workspace), "config", "user.name", "Developer")
        remote.run("-C", str(workspace), "config", "user.email", "developer@example.test")
        remote.run("-C", str(workspace), "config", "core.hooksPath", ".githooks")
        remote.run(
            "-C",
            str(workspace),
            "remote",
            "set-url",
            "origin",
            f"https://x-access-token:{OLD_TOKEN}@github.com/org/repo",
        )
        home = tmp_path / "worker-home"
        home.mkdir()
        shutil.copyfile(remote.config_path, home / ".gitconfig")
        credential_path = home / ".config" / "codegen" / "git-credentials"
        monkeypatch.setattr(git_ops, "GIT_CREDENTIAL_PATH", str(credential_path))
        monkeypatch.setenv("PATH", remote.environment["PATH"])
        # The endpoint is in HOME config, so no GIT_* variable is needed by
        # either native Git or the actual wrapper's filtered subprocess.
        monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
        for index, token in enumerate((TOKEN, "worker-harmless-refreshed-token-canary")):
            remote.token = token
            docker = LocalDocker(workspace, home)
            assert await git_ops.refresh_git_token(docker, "worker", "org/repo", token, "worker-id")
            assert await git_ops.checkout_branch(
                docker, "worker", "story/manager", "worker-id", secret_values=(token,)
            )
            assert (
                remote.run("-C", str(workspace), "rev-parse", "--abbrev-ref", "@{upstream}")
                == "origin/story/manager"
            )
            agent_env = build_agent_subprocess_env(
                {**os.environ, "HOME": str(home), "GITHUB_TOKEN": token, "GH_TOKEN": token}
            )
            assert not any(name.startswith("GIT_") for name in agent_env)
            start = len(remote.headers)
            for command in (
                ["git", "fetch", "origin"],
                ["git", "push", "origin", f"HEAD:story/{index}"],
            ):
                result = subprocess.run(
                    command, cwd=workspace, env=agent_env, capture_output=True, timeout=15
                )
                assert result.returncode == 0, result.stderr
            expected = "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()
            authenticated = [header for header in remote.headers[start:] if header]
            assert authenticated and all(header == expected for header in authenticated)
            assert remote.run(
                "-C", str(remote.remote), "rev-parse", f"story/{index}"
            ) == remote.run("-C", str(workspace), "rev-parse", "HEAD")
            # Agent push retains the product's hook, unlike manager Git.
            assert (workspace / "hook-ran").read_text().count("product-hook") == index + 1
            scoped = subprocess.run(
                ["git", "credential", "fill"],
                cwd=workspace,
                env=agent_env,
                input="protocol=https\nhost=github.com\npath=another/repo.git\n\n",
                text=True,
                capture_output=True,
                timeout=10,
            )
            assert scoped.returncode != 0 and token not in scoped.stdout + scoped.stderr
            exposed = (
                (workspace / ".git" / "config").read_text()
                + (home / ".gitconfig").read_text()
                + str(remote.argv())
            )
            assert token not in exposed and expected not in exposed and OLD_TOKEN not in exposed
        assert not list(home.glob("tmp*")) and not list(credential_path.parent.glob("tmp*"))


@pytest.mark.asyncio
async def test_native_container_creation_error_cannot_echo_github_environment(capsys):
    redis = aioredis.FakeRedis(decode_responses=True)
    docker = _docker_mock()
    encoded = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    docker.run_container.side_effect = RuntimeError(
        f"Docker refused environment: {TOKEN} {encoded}"
    )
    manager = WorkerManager(redis=redis, docker_client=docker)
    with pytest.raises(RuntimeError) as failure:
        await manager.create_worker(
            "worker-id",
            "worker:test",
            ownership=_OWNERSHIP,
            env_vars={"GITHUB_TOKEN": TOKEN, "GH_TOKEN": TOKEN},
            publish_ready=False,
        )
    captured = capsys.readouterr()
    observable = captured.out + captured.err + "".join(traceback.format_exception(failure.value))
    observable += await redis.get("worker:error:worker-id")
    assert "Docker refused environment" in observable
    assert TOKEN not in observable and encoded not in observable
