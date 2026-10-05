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
        if args == ["git", "remote", "get-url", "origin"]:
            return 0, "https://github.com/owner/notes", ""
        if Path(args[0]).name == "python":
            return 0, json.dumps(proof), ""
        if Path(args[0]).name == "kit":
            (root / "installed.txt").write_text("released closure\n")
            return 0, "", ""
        if args[0] == "make" or Path(args[0]).name == "mypy":
            return 0, "", ""
        return await _run_cmd(args, **kwargs)

    monkeypatch.setattr("src.install._run_cmd", command)
    return root, remote, calls, command


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
    assert [args for args in calls if args[0] == "make"] == [
        ["make", "generate-from-spec"],
        ["make", "validate-specs"],
        ["make", "tests"],
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
        return (1, "", "response lost") if args[:2] == ["git", "push"] else result

    monkeypatch.setattr("src.install._run_cmd", lost)
    result = await execute(product, tmp_path)
    assert git(remote, "rev-parse", "refs/heads/story/story-1") == result.head_sha
    assert git(root, "rev-list", "--count", "HEAD") == "2"


@pytest.mark.asyncio
async def test_unknown_push_retains_commit_for_operator_recovery(product, tmp_path, monkeypatch):
    root, _, _, original = product

    async def failed(args, **kwargs):
        if args[:2] == ["git", "push"]:
            return 1, "", "fake-token could not push"
        return await original(args, **kwargs)

    monkeypatch.setattr("src.install._run_cmd", failed)
    with pytest.raises(InstallExecutionError, match="push_outcome_unknown") as caught:
        await execute(product, tmp_path)
    assert caught.value.head_sha == git(root, "rev-parse", "HEAD")
    assert "fake-token" not in str(caught.value)
    assert git(root, "status", "--porcelain") == ""


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
