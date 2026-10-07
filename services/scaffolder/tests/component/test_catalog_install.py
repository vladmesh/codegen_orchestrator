"""Production executor with real local Git; only kit/process product edges are controlled."""

import asyncio
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.install import InstallExecutionError, run_install
from src.scaffold import _run_cmd
from tests.unit.test_install_executor import message

# Every test here starts processes: CI runs this file, the host profile skips it.
pytestmark = pytest.mark.subprocess


def git(root, *args):
    return (
        subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL)
        .decode()
        .strip()
    )


@pytest.fixture
def product(tmp_path, monkeypatch):
    root = tmp_path / "repo-1"
    root.mkdir()
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@localhost")
    (root / "services/tg_bot/src/handlers").mkdir(parents=True)
    (root / "services/tg_bot/src/handlers/notes.py").write_text("notes = 'retained'\n")
    (root / "services/tg_bot/src/main.py").write_text("registered_notes = True\n")
    (root / "services/tg_bot/src/menu.py").write_text("commands = ['note']\n")
    (root / ".gitignore").write_text("*.rej\n.venv/\n")
    git(root, "add", "-A")
    git(root, "commit", "-m", "Owned notes")
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "origin", "main")
    git(root, "fetch", "origin")
    calls = []
    proof = {
        "core": "2.2.0",
        "tooling": {"vcs_info": {"commit_id": "c" * 40}},
        "binding_sha256": "a" * 64,
        "distributions": {"reminders": {"version": "0.5.0"}, "textparse": {"version": "0.1.0"}},
        "component_sources": [
            {"name": "reminders", "target": "d" * 40},
            {"name": "textparse", "target": "e" * 40},
        ],
    }

    async def command(args, **kwargs):
        calls.append(args)
        if git_operation(args) == ["git", "remote", "get-url", "origin"]:
            return 0, "https://github.com/owner/notes", ""
        if Path(args[0]).name == "python":
            return 0, json.dumps(proof), ""
        if Path(args[0]).name == "kit":
            (root / "installed.txt").write_text("released closure\n")
            return 0, "", ""
        if args[0] == "make" or Path(args[0]).name in {"mypy", "ruff"}:
            return 0, "", ""
        return await _run_cmd(args, **kwargs)

    monkeypatch.setattr("src.install._run_cmd", command)
    return root, remote, calls, command


def git_operation(args):
    return ["git", *args[3:]] if args[:3] == ["git", "-c", "core.hooksPath=/dev/null"] else args


async def execute(product, tmp_path, fence=None):
    return await run_install(
        message(),
        SimpleNamespace(workspace_base_path=str(tmp_path)),
        "https://github.com/owner/notes",
        "fake-token",
        fence or AsyncMock(),
    )


@pytest.mark.asyncio
async def test_fixed_closure_preserves_notes_and_publishes_verified_exact_head(product, tmp_path):
    root, remote, calls, _ = product
    notes = (root / "services/tg_bot/src/handlers/notes.py").read_bytes()
    fence = AsyncMock()
    result = await execute(product, tmp_path, fence)
    assert git(remote, "rev-parse", "refs/heads/story/story-1") == result.head_sha
    assert git(root, "status", "--porcelain") == ""
    assert (root / "services/tg_bot/src/handlers/notes.py").read_bytes() == notes
    kit = str(root / ".venv/bin/kit")
    assert [args for args in calls if args[0] == kit] == [
        [kit, "add", "reminders"],
        [kit, "add", "textparse"],
        [kit, "bind", "reminders", "--default"],
    ]
    assert [args for args in calls if args[0] == "make" or Path(args[0]).name == "ruff"] == [
        ["make", "generate-from-spec"],
        [
            str(root / ".venv/bin/ruff"),
            "format",
            "--exclude",
            "*.md,.venv/**,**/.venv/**,services/**/migrations/**",
            ".",
        ],
        ["make", "validate-specs"],
        ["make", "tests", "REDIS_URL=redis://redis.invalid:6379"],
    ]
    assert [args for args in calls if Path(args[0]).name == "mypy"] == [
        [str(root / f"services/{service}/.venv/bin/mypy"), f"services/{service}"]
        for service in ("backend", "tg_bot")
    ]
    published = fence.call_args.args[0]
    assert published.action == "publish" and published.head_sha == result.head_sha
    assert published.verification.distributions == {"reminders": "0.5.0", "textparse": "0.1.0"}


@pytest.mark.asyncio
async def test_lost_push_response_is_read_back_without_duplicate_commit(
    product, tmp_path, monkeypatch
):
    root, remote, _, original = product

    async def lost(args, **kwargs):
        result = await original(args, **kwargs)
        return (1, "", "response lost") if git_operation(args)[:2] == ["git", "push"] else result

    monkeypatch.setattr("src.install._run_cmd", lost)
    result = await execute(product, tmp_path)
    assert git(remote, "rev-parse", "refs/heads/story/story-1") == result.head_sha
    assert git(root, "rev-list", "--count", "HEAD") == "2"


@pytest.mark.asyncio
async def test_unknown_push_retains_commit_for_operator_recovery(product, tmp_path, monkeypatch):
    root, _, _, original = product

    async def failed(args, **kwargs):
        if git_operation(args)[:2] == ["git", "push"]:
            return 1, "", "fake-token could not push"
        return await original(args, **kwargs)

    monkeypatch.setattr("src.install._run_cmd", failed)
    with pytest.raises(InstallExecutionError, match="push_outcome_unknown") as caught:
        await execute(product, tmp_path)
    assert caught.value.head_sha == git(root, "rev-parse", "HEAD")
    assert "fake-token" not in str(caught.value)
    assert git(root, "status", "--porcelain") == ""


@pytest.mark.asyncio
async def test_infrastructure_git_bypasses_enabled_product_hooks(product, tmp_path):
    root, remote, calls, _ = product
    hooks = root / ".githooks"
    hooks.mkdir()
    marker = root / ".git/hook-invoked"
    canary = hooks / "pre-push"
    canary.write_text("#!/bin/sh\ntouch .git/hook-invoked\nexit 91\n")
    canary.chmod(0o755)
    git(root, "config", "core.hooksPath", ".githooks")
    git(root, "add", "-A")
    git(root, "commit", "-m", "Product hook canary")
    git(root, "-c", "core.hooksPath=/dev/null", "push", "origin", "main")
    plain = subprocess.run(["git", "push", "origin", "main"], cwd=root, capture_output=True)
    assert plain.returncode != 0 and marker.exists()
    marker.unlink()
    result = await execute(product, tmp_path)
    assert not marker.exists()
    assert git(root, "config", "core.hooksPath") == ".githooks"
    assert git(remote, "rev-parse", "refs/heads/story/story-1") == result.head_sha
    assert all(
        args[1:3] == ["-c", "core.hooksPath=/dev/null"] for args in calls if args[0] == "git"
    )


@pytest.mark.asyncio
async def test_only_owned_remote_git_receives_install_credential(product, tmp_path, monkeypatch):
    _, _, _, original = product
    observed = []

    async def inspect(args, **kwargs):
        authenticated = any(
            key.startswith("GIT_CONFIG_VALUE_") and value.startswith("Authorization: ")
            for key, value in kwargs["env"].items()
        )
        observed.append((git_operation(args), authenticated))
        return await original(args, **kwargs)

    monkeypatch.setattr("src.install._run_cmd", inspect)
    await execute(product, tmp_path)
    for args, authenticated in observed:
        assert authenticated == (
            args[:2] in (["git", "fetch"], ["git", "ls-remote"], ["git", "push"])
        )
    assert any(Path(args[0]).name == "mypy" for args, _ in observed)
    assert any(Path(args[0]).name == "kit" and args[1] == "add" for args, _ in observed)


@pytest.mark.parametrize("filename", ["main.py", "menu.py"])
@pytest.mark.asyncio
async def test_owned_bot_application_mutation_refuses_publication(
    product, tmp_path, monkeypatch, filename
):
    root, remote, _, original = product

    async def mutate(args, **kwargs):
        result = await original(args, **kwargs)
        if args == ["make", "generate-from-spec"]:
            (root / f"services/tg_bot/src/{filename}").write_text("overwritten = True\n")
        return result

    monkeypatch.setattr("src.install._run_cmd", mutate)
    with pytest.raises(InstallExecutionError, match="protected_files_changed"):
        await execute(product, tmp_path)
    assert git(remote, "ls-remote", str(remote), "refs/heads/story/story-1") == ""


@pytest.mark.asyncio
async def test_ignored_copier_rejection_refuses_before_branch_or_product_mutation(
    product, tmp_path
):
    root, _, calls, _ = product
    (root / "custom.py.rej").write_text("unresolved update\n")
    base = git(root, "rev-parse", "HEAD")
    with pytest.raises(InstallExecutionError, match="update_unresolved"):
        await execute(product, tmp_path)
    assert git(root, "rev-parse", "HEAD") == base
    assert not any(Path(args[0]).name == "kit" for args in calls)


@pytest.mark.asyncio
async def test_process_group_cancellation_kills_owned_children(tmp_path):
    marker = tmp_path / "child-survived"
    command = asyncio.create_task(
        _run_cmd(
            ["bash", "-c", 'sleep 0.4; touch "$1"', "owned", str(marker)],
            cwd=tmp_path,
            timeout=5,
            kill_process_group=True,
        )
    )
    await asyncio.sleep(0.05)
    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command
    await asyncio.sleep(0.5)
    assert not marker.exists()
