"""Fixed operation argv and refusal precede product mutation."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.contracts.queues.scaffold import ScaffoldMessage
from src.install import InstallExecutionError, run_install


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
        if args[:3] == ["git", "status", "--porcelain"]:
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
