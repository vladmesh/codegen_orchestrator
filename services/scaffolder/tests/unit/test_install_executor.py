"""Fixed operation argv and refusal precede product mutation."""

from datetime import UTC, datetime
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.contracts.queues.scaffold import ScaffoldMessage
from src.install import (
    InstallExecutionError,
    install_environment,
    product_environment,
    protected_files,
    run_install,
)


def test_product_environment_discards_inherited_credentials(tmp_path, monkeypatch):
    for name in ("GIT_CONFIG_VALUE_0", "GIT_CONFIG_VALUE_1", "GITHUB_TOKEN", "SECRET_KEY"):
        monkeypatch.setenv(name, "synthetic-parent-secret")
    env = product_environment(tmp_path)
    # The only git configuration a product command gets is trust in its own checkout.
    assert {key: value for key, value in env.items() if key.startswith("GIT_CONFIG_")} == {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": str(tmp_path),
    }
    assert "synthetic-parent-secret" not in env.values()


def test_bot_owned_sources_are_protected_and_generated_output_is_mutable(tmp_path):
    tracked = [
        "services/tg_bot/src/main.py",
        "services/tg_bot/src/menu.py",
        "services/tg_bot/src/generated/bindings.py",
    ]
    for name in tracked:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("baseline\n")
    assert set(protected_files(tmp_path, tracked)) == set(tracked[:2])


@pytest.mark.parametrize("suffix", ["", ".git"])
@pytest.mark.parametrize(
    "foreign",
    [
        "https://github.com/vladmesh/codegen-product-kit.git",
        "https://github.com/owner/notes-fork.git",
    ],
)
def test_product_auth_does_not_reach_the_public_catalog(tmp_path, suffix, foreign):
    git_url = "https://github.com/owner/notes"
    env = install_environment("synthetic-token", tmp_path, git_url)
    owned = subprocess.run(
        ["git", "config", "--get-urlmatch", "http.extraheader", git_url + suffix],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert owned.returncode == 0 and owned.stdout.startswith("Authorization: Basic ")
    catalog = subprocess.run(
        [
            "git",
            "config",
            "--get-urlmatch",
            "http.extraheader",
            foreign,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert catalog.returncode == 1 and catalog.stdout == ""


def message():
    return ScaffoldMessage(
        project_id="00000000-0000-0000-0000-000000000001",
        repository_id="repo-1",
        mode="install",
        template_repo="gh:vladmesh/codegen-product-kit",
        template_ref="release-test",
        project_name="notes",
        modules="backend,tg_bot",
        task_id="task-1",
        story_id="story-1",
        operation_id="install-1",
        cycle_started_at=datetime.now(UTC),
        install={
            "package": {
                "name": "reminders",
                "distribution": "codegen-kit-reminders",
                "version": "0.5.0",
                "tag": "packages/reminders/v0.5.0",
            },
            "libraries": [
                {
                    "name": "textparse",
                    "distribution": "codegen-kit-textparse",
                    "version": "0.1.0",
                    "tag": "packages/textparse/v0.1.0",
                }
            ],
            "binding": {
                "package": "reminders",
                "resource": "codegen_kit_reminders:bindings/default.yaml",
                "sha256": "a" * 64,
                "functions": ["textparse.when"],
            },
            "core_version": "2.2.0",
            "python_version": "3.12.0",
            "catalog_digest": "b" * 64,
            "tooling_commit": "c" * 40,
        },
    )


@pytest.mark.asyncio
async def test_missing_workspace_refuses_without_any_command(tmp_path, monkeypatch):
    command = AsyncMock()
    monkeypatch.setattr("src.install._run_cmd", command)
    with pytest.raises(InstallExecutionError, match="workspace_unowned"):
        await run_install(
            message(),
            SimpleNamespace(workspace_base_path=str(tmp_path)),
            "https://github.com/owner/notes",
            "fake-token",
            AsyncMock(),
        )
    command.assert_not_awaited()


@pytest.mark.asyncio
async def test_dirty_checkout_is_retained_and_never_runs_kit(tmp_path, monkeypatch):
    (tmp_path / "repo-1/.git").mkdir(parents=True)
    calls = []

    async def command(args, **kwargs):
        calls.append(args)
        if "status" in args and "--porcelain" in args:
            return 0, " M notes.py\n", ""
        return 0, "", ""

    monkeypatch.setattr("src.install._run_cmd", command)
    with pytest.raises(InstallExecutionError, match="workspace_dirty"):
        await run_install(
            message(),
            SimpleNamespace(workspace_base_path=str(tmp_path)),
            "https://github.com/owner/notes",
            "fake-token",
            AsyncMock(),
        )
    assert all("kit" not in Path(args[0]).name for args in calls)


def foreign_owned_checkout(root):
    """A real checkout git treats as owned by another user, as a worker-owned workspace is."""
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    (root / "notes.py").write_text("dirty\n")
    return {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}


@pytest.mark.asyncio
async def test_install_git_trusts_the_worker_owned_workspace(tmp_path, monkeypatch):
    root = tmp_path / "repo-1"
    foreign = foreign_owned_checkout(root)
    real_run_cmd = run_install.__globals__["_run_cmd"]

    async def as_root_on_worker_checkout(args, *, env, **kwargs):
        return await real_run_cmd(args, env=env | foreign, **kwargs)

    monkeypatch.setattr("src.install._run_cmd", as_root_on_worker_checkout)
    with pytest.raises(InstallExecutionError, match="workspace_dirty"):
        await run_install(
            message(),
            SimpleNamespace(workspace_base_path=str(tmp_path)),
            "https://github.com/owner/notes",
            "fake-token",
            AsyncMock(),
        )


@pytest.mark.parametrize("build", ["product", "install"])
def test_product_tool_git_trusts_the_worker_owned_workspace(tmp_path, build):
    root = tmp_path / "repo-1"
    foreign = foreign_owned_checkout(root)
    env = (
        product_environment(root)
        if build == "product"
        else install_environment("synthetic-token", root, "https://github.com/owner/notes")
    )
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        env=env | foreign,
        capture_output=True,
        text=True,
        check=False,
    )
    assert status.returncode == 0, status.stderr
