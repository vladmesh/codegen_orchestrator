"""The manager's own git never runs the product's hooks.

Production evidence (sprint:1445, 2026-09-17, project ed689144, story
story-ea07a289): the developer workspace is reused between the stories of one
project, and the first story's `make setup` leaves `core.hooksPath=.githooks`
in it. The manager's `git push -u` for the *second* story therefore ran the
generated product's `pre-push` hook, which — with no upstream yet and a stale
`origin/main` — linted and tested every service, blew the 30 s exec bound, and
failed worker creation. Five engineering runs died the same way, deterministically.

These tests run the real script against a real local repository with a
deliberately fatal product hook installed: no network, a bare repo for the
remote, a fraction of a second.
"""

from __future__ import annotations

import base64
from pathlib import Path
import re
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

from fakeredis import aioredis
import pytest

from shared.contracts.queues.worker import AgentType, WorkerOwnership
from src import git_ops
from src.manager import WorkerManager

pytestmark = pytest.mark.asyncio

_OWNERSHIP = WorkerOwnership(
    story_id="story-ea07a289", project_id="proj-1", run_id="live-1", attempt_id="eng-1"
)

# A git command word: `git` at the start of a word, not `.git` or `origin/git`.
_GIT_COMMAND = re.compile(r"(?<![\w/.-])git\s+")
_HOOKLESS = "-c core.hooksPath=/dev/null "


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30, check=True
    )
    return result.stdout.strip()


def _make_product_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A bare remote with `main`, and a workspace clone carrying product hooks.

    The workspace is the scaffolder's: `.githooks/pre-push` installed and
    `core.hooksPath` pointing at it, exactly as the product's `make setup`
    leaves it after the first story.
    """
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--initial-branch=main")
    _git(seed, "config", "user.email", "ai@codegen.local")
    _git(seed, "config", "user.name", "Codegen Bot")
    (seed / "README.md").write_text("# product\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "first story")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")

    workspace = tmp_path / "workspace"
    _git(tmp_path, "clone", str(remote), str(workspace))
    _git(workspace, "config", "user.email", "ai@codegen.local")
    _git(workspace, "config", "user.name", "Codegen Bot")
    hooks = workspace / ".githooks"
    hooks.mkdir()
    pre_push = hooks / "pre-push"
    # The product's hook, reduced to its effect on the manager: it runs, and it
    # takes the push down with it.
    pre_push.write_text("#!/bin/sh\necho 'product pre-push ran' >&2\nexit 1\n")
    pre_push.chmod(0o755)
    _git(workspace, "config", "core.hooksPath", ".githooks")
    return remote, workspace


def _run_checkout_script(workspace: Path, branch: str) -> subprocess.CompletedProcess[str]:
    """Run the real script, with only its hard-coded `/workspace` redirected."""
    script = git_ops.build_checkout_script(branch)
    assert "cd /workspace" in script
    script = script.replace("cd /workspace", f"cd {workspace}", 1)
    return subprocess.run(  # noqa: S603
        ["bash", "-c", script], capture_output=True, text=True, timeout=60
    )


def _advance_remote(remote: Path, tmp_path: Path, branch: str, message: str) -> str:
    """Push one commit to `branch` of the remote from a throwaway clone."""
    side = tmp_path / f"side-{message.replace(' ', '-')}"
    _git(tmp_path, "clone", str(remote), str(side))
    _git(side, "config", "user.email", "ai@codegen.local")
    _git(side, "config", "user.name", "Codegen Bot")
    if branch != "main":
        _git(side, "checkout", "-B", branch)
    (side / f"{message.replace(' ', '_')}.txt").write_text(message)
    _git(side, "add", ".")
    _git(side, "commit", "-m", message)
    _git(side, "push", "-u", "origin", branch)
    return _git(side, "rev-parse", "HEAD")


# --- Criterion 1: no product hook on an infrastructure command ---


async def test_every_git_invocation_in_the_checkout_script_is_hook_free():
    script = git_ops.build_checkout_script("story/story-ea07a289")

    invocations = list(_GIT_COMMAND.finditer(script))
    assert invocations, "the checkout script runs no git at all"
    for match in invocations:
        rest = script[match.end() :]
        assert rest.startswith(_HOOKLESS), (
            "a git invocation in the checkout script does not neutralise the workspace's "
            f"hooks path: ...{script[max(0, match.start() - 40) : match.end() + 40]}..."
        )


async def test_the_checkout_script_does_not_write_the_workspace_git_config():
    """Per-command `-c`, never a write: the product's hooks stay installed."""
    script = git_ops.build_checkout_script("story/story-ea07a289")

    assert "config core.hooksPath" not in script
    assert "config --unset" not in script


async def test_the_upstream_push_is_not_forced_to_success():
    script = git_ops.build_checkout_script("story/story-ea07a289")

    push_line = next(line for line in script.splitlines() if " push " in line)
    assert "|| true" not in push_line
    assert "2>/dev/null" not in push_line


async def test_a_product_pre_push_hook_does_not_run_and_stays_installed(tmp_path):
    """The behavioural half of criteria 1 and 5, against real git."""
    remote, workspace = _make_product_repo(tmp_path)

    result = _run_checkout_script(workspace, "story/story-ea07a289")

    assert result.returncode == 0, result.stderr
    assert "product pre-push ran" not in result.stderr
    # Criterion 5: the developer agent's own commits still get the product's hooks.
    assert _git(workspace, "config", "--get", "core.hooksPath") == ".githooks"
    assert (workspace / ".githooks" / "pre-push").exists()
    # The upstream the checkout promised was actually written.
    assert (
        _git(workspace, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
        == "origin/story/story-ea07a289"
    )
    assert "story/story-ea07a289" in _git(remote, "branch", "--list", "story/story-ea07a289")


# --- Criterion 2: the base of a new branch, and the resume of an existing one ---


async def test_a_new_story_branch_is_cut_from_the_fetched_default_branch(tmp_path):
    """The workspace's HEAD and its `origin/main` are both stale; main is not."""
    remote, workspace = _make_product_repo(tmp_path)
    # The first story's branch, still checked out in the reused workspace.
    _git(workspace, "checkout", "-b", "story/first")
    (workspace / "first.txt").write_text("first story")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "first story work")
    stale_head = _git(workspace, "rev-parse", "HEAD")
    # main moved on the remote and the workspace has not seen it.
    real_main = _advance_remote(remote, tmp_path, "main", "main moved on")
    assert _git(workspace, "rev-parse", "origin/main") != real_main

    result = _run_checkout_script(workspace, "story/story-ea07a289")

    assert result.returncode == 0, result.stderr
    assert _git(workspace, "rev-parse", "--abbrev-ref", "HEAD") == "story/story-ea07a289"
    assert _git(workspace, "rev-parse", "HEAD") == real_main
    assert _git(workspace, "rev-parse", "HEAD") != stale_head


async def test_an_existing_story_branch_resumes_at_its_remote_tip(tmp_path):
    remote, workspace = _make_product_repo(tmp_path)
    tip = _advance_remote(remote, tmp_path, "story/story-ea07a289", "earlier turn")
    main_tip = _advance_remote(remote, tmp_path, "main", "main moved on")

    result = _run_checkout_script(workspace, "story/story-ea07a289")

    assert result.returncode == 0, result.stderr
    assert _git(workspace, "rev-parse", "--abbrev-ref", "HEAD") == "story/story-ea07a289"
    # At the remote tip, and neither reset nor rebased onto the default branch.
    assert _git(workspace, "rev-parse", "HEAD") == tip
    assert _git(workspace, "rev-parse", "HEAD") != main_tip
    assert (
        _git(workspace, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
        == "origin/story/story-ea07a289"
    )


async def test_an_unpushed_local_commit_on_a_resumed_branch_is_never_discarded(tmp_path):
    """Resuming is not a reset: the developer's own tip survives."""
    remote, workspace = _make_product_repo(tmp_path)
    _advance_remote(remote, tmp_path, "story/story-ea07a289", "earlier turn")
    _run_checkout_script(workspace, "story/story-ea07a289")
    (workspace / "unpushed.txt").write_text("committed but not pushed")
    _git(workspace, "add", ".")
    _git(workspace, "-c", "core.hooksPath=/dev/null", "commit", "-m", "local work")
    local_tip = _git(workspace, "rev-parse", "HEAD")

    result = _run_checkout_script(workspace, "story/story-ea07a289")

    assert result.returncode == 0, result.stderr
    assert _git(workspace, "rev-parse", "HEAD") == local_tip


# --- Criterion 3: the token refresh runs no product hook either ---


async def test_the_token_refresh_script_is_hook_free():
    script = git_ops.build_token_refresh_script("org/repo", "ghs-token")

    invocations = list(_GIT_COMMAND.finditer(script))
    assert invocations
    for match in invocations:
        assert script[match.end() :].startswith(_HOOKLESS)
    assert "config core.hooksPath" not in script


async def test_refresh_git_token_execs_the_hook_free_script():
    docker = MagicMock()
    docker.exec_in_container = AsyncMock(return_value=(0, ""))

    assert await git_ops.refresh_git_token(docker, "cid", "org/repo", "ghs-token", "w-1")

    decoded = _decoded_script(docker.exec_in_container.await_args.args[1])
    assert git_ops.GIT in decoded
    for match in _GIT_COMMAND.finditer(decoded):
        assert decoded[match.end() :].startswith(_HOOKLESS)


def _decoded_script(cmd: str) -> str:
    payload = cmd.split("echo ", 1)[1].split(" |")[0].strip()
    return base64.b64decode(payload).decode()


async def test_checkout_branch_execs_the_hook_free_script():
    docker = MagicMock()
    docker.exec_capture = AsyncMock(return_value=(0, b"", b""))

    assert await git_ops.checkout_branch(docker, "cid", "story/story-ea07a289", "w-1")

    decoded = _decoded_script(docker.exec_capture.await_args.args[1])
    assert decoded == git_ops.build_checkout_script("story/story-ea07a289")


async def test_checkout_branch_reports_a_failed_script():
    docker = MagicMock()
    docker.exec_capture = AsyncMock(return_value=(1, b"", b"fatal: no upstream"))

    result = await git_ops.checkout_branch(docker, "cid", "story/story-ea07a289", "w-1")

    assert not result
    assert "exit_code=1" in result.detail
    assert "stderr: fatal: no upstream" in result.detail


# --- Criterion 4: a checkout that did not do its job fails worker creation ---


def _docker_mock() -> MagicMock:
    wrapper = MagicMock()
    wrapper.image_exists = AsyncMock(return_value=True)
    wrapper.get_image_label = AsyncMock(return_value="basehash0001")
    wrapper.remove_container = AsyncMock()
    container = MagicMock()
    container.id = "test-id"
    wrapper.run_container = AsyncMock(return_value=container)
    wrapper.exec_in_container = AsyncMock(return_value=(0, b""))
    wrapper.create_network = AsyncMock()
    wrapper.connect_network = AsyncMock()
    wrapper.remove_network = AsyncMock()
    wrapper.get_container_logs = AsyncMock(return_value="")
    return wrapper


async def test_a_false_checkout_fails_creation_before_any_material_is_injected():
    redis = aioredis.FakeRedis(decode_responses=True)
    manager = WorkerManager(redis=redis, docker_client=_docker_mock())

    with (
        patch("src.manager.settings") as mock_settings,
        patch.object(
            manager, "ensure_or_build_image", new_callable=AsyncMock, return_value="worker:latest"
        ),
        patch(
            "src.manager.workspace_mod.get_scaffolded_workspace",
            return_value=(Path("/data/ws/repo-1"), True),
        ),
        patch(
            "src.manager.git_ops.checkout_branch",
            new_callable=AsyncMock,
            return_value=git_ops.CheckoutResult(ok=False, detail="exit_code=1; stderr: fatal"),
        ),
        patch.object(manager, "_register_broker_worker", new_callable=AsyncMock),
        patch.object(manager, "_inject_worker_materials", new_callable=AsyncMock) as inject,
    ):
        mock_settings.ENVIRONMENT = "production"
        mock_settings.DOCKER_NETWORK = ""
        mock_settings.WORKER_NETWORK = "codegen_worker"
        mock_settings.SCAFFOLDED_WORKSPACE_PATH = "/data/ws"
        mock_settings.WORKER_BROKER_URL = "http://worker-broker:8001"
        mock_settings.WORKER_SUBPROCESS_TIMEOUT_SECONDS = 300
        mock_settings.WORKER_IMAGE_PREFIX = "worker"
        mock_settings.WORKER_DOCKER_LABELS = "{}"
        mock_settings.WORKER_TRANSCRIPT_STORAGE_PATH = "/data/worker-transcripts"
        mock_settings.WORKER_TRANSCRIPT_MAX_BYTES = 5 * 1024 * 1024
        mock_settings.WORKER_TRANSCRIPT_RETENTION_DAYS = 30

        with pytest.raises(RuntimeError):
            await manager.create_worker_with_capabilities(
                worker_id="dev-p-checkout-false",
                capabilities=["git"],
                base_image="worker-base:latest",
                ownership=_OWNERSHIP,
                agent_type=AgentType.CLAUDE,
                auth_mode="api_key",
                api_key="test-api-key",
                instructions="required instructions",
                repo_id="repo-1",
                branch="story/story-ea07a289",
            )

    inject.assert_not_awaited()
    recorded = await redis.get("worker:error:dev-p-checkout-false")
    assert recorded.strip()
    assert "checkout_branch" in recorded
    # The script's own account reaches the record the spawner reads.
    assert "exit_code=1; stderr: fatal" in recorded
