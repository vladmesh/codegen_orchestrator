"""Production executor with real local Git; only kit/process product edges are controlled."""

import asyncio
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.workspace_preservation import CATALOG_INSTALL_ATTEMPTS, has_preserved_work
from src.install import InstallExecutionError, run_install
from src.scaffold import _run_cmd
from tests.unit.test_install_executor import check_install, glue_item, message

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
        if Path(args[0]).name == "kit" and args[1] == "check-install":
            return 0, json.dumps(check_install()), ""
        if Path(args[0]).name == "kit":
            with (Path(kwargs["cwd"]) / "installed.txt").open("a") as installed:
                installed.write("released closure\n")
            return 0, "", ""
        if args[0] in {"make", "sh"} or Path(args[0]).name == "mypy":
            return 0, "", ""
        return await _run_cmd(args, **kwargs)

    monkeypatch.setattr("src.install._run_cmd", command)
    return root, remote, calls, command


def git_operation(args):
    return ["git", *args[3:]] if args[:3] == ["git", "-c", "core.hooksPath=/dev/null"] else args


async def execute(product, tmp_path, fence=None, operation="install-1"):
    return await run_install(
        message().model_copy(update={"operation_id": operation}),
        SimpleNamespace(workspace_base_path=str(tmp_path)),
        "https://github.com/owner/notes",
        "fake-token",
        fence or AsyncMock(),
    )


def attempt(tmp_path, operation="install-1"):
    return tmp_path / CATALOG_INSTALL_ATTEMPTS / "repo-1" / operation


@pytest.mark.asyncio
async def test_fixed_closure_preserves_notes_and_publishes_verified_exact_head(product, tmp_path):
    root, remote, calls, _ = product
    notes = (root / "services/tg_bot/src/handlers/notes.py").read_bytes()
    fence = AsyncMock()
    result = await execute(product, tmp_path, fence)
    assert git(remote, "rev-parse", "refs/heads/story/story-1") == result.head_sha
    installed = git(remote, "show", f"{result.head_sha}:installed.txt")
    assert installed.splitlines() == ["released closure"] * 3  # add, add, bind
    assert git(root, "status", "--porcelain") == ""
    assert (root / "services/tg_bot/src/handlers/notes.py").read_bytes() == notes
    # The published attempt's checkout is gone, after every command of it returned.
    assert result.checkout == "repo-1/install-1" and result.checkout_removed
    assert not attempt(tmp_path).exists()
    assert "install-1" not in git(root, "worktree", "list")
    checkout = attempt(tmp_path).resolve()
    kit = str(checkout / ".venv/bin/kit")
    # Every component is added from the payload's pinned catalog commit, never the default.
    pinned = ["--catalog-source", "https://github.com/vladmesh/codegen-product-kit.git"]
    pinned += ["--catalog-ref", "d" * 40]
    assert [args for args in calls if args[0] == kit] == [
        [kit, "check-install", "reminders", "--json", *pinned, "--version", "0.5.0"]
        + ["--product-root", str(checkout)],
        [kit, "add", "reminders", *pinned],
        [kit, "add", "textparse", *pinned],
        [kit, "bind", "reminders", "--default"],
    ]
    assert [args for args in calls if args[0] == "make"] == [
        ["make", "generate-from-spec"],
        ["make", "validate-specs"],
        ["make", "tests", "REDIS_URL=redis://redis.invalid:6379"],
    ]
    assert [args for args in calls if Path(args[0]).name == "mypy"] == [
        [str(checkout / f"services/{service}/.venv/bin/mypy"), f"services/{service}"]
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
    assert git(remote, "rev-list", "--count", "refs/heads/story/story-1") == "2"


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
    # The exact unpublished head stays in the operation's own checkout, for review.
    assert caught.value.head_sha == git(attempt(tmp_path), "rev-parse", "HEAD")
    assert "fake-token" not in str(caught.value)
    assert git(attempt(tmp_path), "status", "--porcelain") == ""
    assert git(root, "status", "--porcelain") == ""
    assert has_preserved_work(root)


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
            checkout = Path(kwargs["cwd"])
            (checkout / f"services/tg_bot/src/{filename}").write_text("overwritten = True\n")
        return result

    monkeypatch.setattr("src.install._run_cmd", mutate)
    with pytest.raises(InstallExecutionError, match="protected_files_changed"):
        await execute(product, tmp_path)
    assert git(remote, "ls-remote", str(remote), "refs/heads/story/story-1") == ""


@pytest.mark.asyncio
async def test_a_dirty_shared_workspace_is_neither_cleaned_nor_overwritten(product, tmp_path):
    """Worker leftovers stay exactly as they are; the install runs in its own checkout."""
    root, remote, calls, _ = product
    (root / "custom.py.rej").write_text("unresolved update\n")
    (root / "services/tg_bot/src/menu.py").write_text("commands = ['edited']\n")
    git(root, "switch", "-c", "story/story-1")
    (root / "unpublished.py").write_text("work\n")
    git(root, "add", "unpublished.py")
    git(root, "commit", "-m", "Unpublished local story work")
    git(root, "switch", "-q", "main")
    (root / "services/tg_bot/src/menu.py").write_text("commands = ['edited']\n")
    local = git(root, "rev-parse", "refs/heads/story/story-1")
    before = {
        path: (root / path).read_bytes()
        for path in ("custom.py.rej", "services/tg_bot/src/menu.py")
    }
    result = await execute(product, tmp_path)
    assert {path: (root / path).read_bytes() for path in before} == before
    assert git(root, "rev-parse", "refs/heads/story/story-1") == local
    # The published story starts at the owned remote base, not at the unpublished branch.
    assert git(remote, "rev-parse", f"{result.head_sha}^") == git(remote, "rev-parse", "main")
    assert result.base_sha == git(remote, "rev-parse", "main")


@pytest.mark.asyncio
async def test_a_tracked_copier_rejection_refuses_before_any_kit_command(product, tmp_path):
    root, remote, calls, _ = product
    (root / "custom.py.rej").write_text("unresolved update\n")
    git(root, "add", "-f", "custom.py.rej")
    git(root, "commit", "-m", "Rejection committed")
    git(root, "push", "origin", "main")
    with pytest.raises(InstallExecutionError, match="update_unresolved"):
        await execute(product, tmp_path)
    assert not any(Path(args[0]).name == "kit" for args in calls)
    assert git(remote, "ls-remote", str(remote), "refs/heads/story/story-1") == ""


@pytest.mark.asyncio
async def test_a_second_operation_starts_at_the_published_story_head_in_a_new_checkout(
    product, tmp_path
):
    _, remote, _, _ = product
    first = await execute(product, tmp_path, operation="install-1")
    second = await execute(product, tmp_path, operation="install-2")
    assert second.base_sha == first.head_sha
    assert git(remote, "rev-parse", "refs/heads/story/story-1") == second.head_sha
    assert second.checkout == "repo-1/install-2" != first.checkout


@pytest.mark.asyncio
async def test_glue_refusal_retains_the_untouched_attempt_and_a_new_attempt_is_fresh(
    product, tmp_path, monkeypatch
):
    root, remote, calls, original = product
    answer = check_install("glue", [glue_item("command_conflict", symbol="handle_remind")])

    async def glue(args, **kwargs):
        if Path(args[0]).name == "kit" and args[1] == "check-install":
            return 3, json.dumps(answer), ""
        return await original(args, **kwargs)

    monkeypatch.setattr("src.install._run_cmd", glue)
    with pytest.raises(InstallExecutionError, match="glue_required") as refused:
        await execute(product, tmp_path, operation="install-1")
    assert refused.value.preflight.status == "glue"
    retained = attempt(tmp_path, "install-1")
    assert retained.is_dir() and git(retained, "status", "--porcelain") == ""
    assert not any(Path(args[0]).name == "kit" and args[1] == "add" for args in calls)
    # Redelivery of the same operation never runs in, or reuses, its retained checkout.
    with pytest.raises(InstallExecutionError, match="attempt_exists"):
        await execute(product, tmp_path, operation="install-1")
    monkeypatch.setattr("src.install._run_cmd", original)
    result = await execute(product, tmp_path, operation="install-2")
    assert result.checkout == "repo-1/install-2"
    assert retained.is_dir() and git(remote, "rev-parse", "refs/heads/story/story-1")


@pytest.mark.asyncio
async def test_cancellation_retains_the_attempt_and_releases_the_workspace(
    product, tmp_path, monkeypatch
):
    root, _, _, original = product
    started = asyncio.Event()

    async def hang(args, **kwargs):
        if args[:2] == ["make", "generate-from-spec"]:
            started.set()
            await asyncio.sleep(30)
        return await original(args, **kwargs)

    monkeypatch.setattr("src.install._run_cmd", hang)
    running = asyncio.create_task(execute(product, tmp_path))
    await asyncio.wait_for(started.wait(), 10)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert (attempt(tmp_path) / "installed.txt").is_file()
    assert has_preserved_work(root)
    # The workspace lock is free again: another operation may claim the repository.
    monkeypatch.setattr("src.install._run_cmd", original)
    assert (await execute(product, tmp_path, operation="install-2")).checkout_removed


@pytest.mark.asyncio
async def test_concurrent_attempts_on_one_repository_have_one_writer(
    product, tmp_path, monkeypatch
):
    _, _, _, original = product
    gate = asyncio.Event()

    async def slow(args, **kwargs):
        if args[:2] == ["make", "generate-from-spec"]:
            await gate.wait()
        return await original(args, **kwargs)

    monkeypatch.setattr("src.install._run_cmd", slow)
    first = asyncio.create_task(execute(product, tmp_path, operation="install-1"))
    await asyncio.sleep(0.5)
    with pytest.raises(InstallExecutionError, match="branch_writer_live"):
        await execute(product, tmp_path, operation="install-2")
    gate.set()
    assert (await first).checkout == "repo-1/install-1"


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
