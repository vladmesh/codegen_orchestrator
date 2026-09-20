"""An engineering run leaves nothing of the orchestrator in the product repository.

Against the *vendored render of the pinned kit*, made a real git repository: the
manager's instruction injection and the wrapper's turn preparation both run, the agent's
turn is then simulated by writing the files a real turn writes, and the commit is taken
through the product's own `git add -A` path. What the commit carries is the assertion.

This is the test that stand run 34203735584 would have failed: its engineering commit
d50f8516 carried sixteen files of orchestrator debris into the generated product, and the
merged product's CI logged `overriding recipe for target worker-start`.
"""

import asyncio
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from worker_wrapper.injected_paths import INJECTED_PATHS, instruction_filename
from worker_wrapper.wrapper import WorkerWrapper, WorkerWrapperConfig

from scripts.template_pin import TEMPLATE_PIN
from shared.constants import WorkerWorkspace
from shared.contracts.queues.worker_result import (
    WorkerCompletedResult,
    WorkerResultStatus,
)
from shared.contracts.vocab import AgentType

ROOT = Path(__file__).resolve().parents[4]
KIT = TEMPLATE_PIN.fixture_path()

# The product's `pre-commit` hook runs `make format` and then `git add -A`. Reduced to the
# effect that matters here, it stages everything the worker left in the tree — which is
# why an ignore rule, and not tidiness, is what keeps the turn's files out of the commit.
PRE_COMMIT = "#!/bin/sh\ngit add -A\n"


def git(workspace: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", *args], cwd=str(workspace), capture_output=True, text=True, timeout=60, check=True
    )
    return result.stdout.strip()


def manager_instruction_paths() -> dict[AgentType, str]:
    """The paths worker-manager actually injects into, read from its own agent configs."""
    sys.path.insert(0, str(ROOT / "services" / "worker-manager"))
    try:
        from src.agents import ClaudeCodeAgent, CodexAgent, FactoryDroidAgent
    finally:
        sys.path.pop(0)
    return {
        AgentType.CLAUDE: ClaudeCodeAgent().get_instruction_path(),
        AgentType.CODEX: CodexAgent().get_instruction_path(),
        AgentType.FACTORY: FactoryDroidAgent().get_instruction_path(),
    }


@pytest.fixture
def product(tmp_path):
    """The pinned kit, rendered and committed, as a worker finds its checkout."""
    workspace = tmp_path / "workspace"
    shutil.copytree(KIT, workspace)
    hooks = workspace / ".githooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text(PRE_COMMIT)
    (hooks / "pre-commit").chmod(0o755)
    git(workspace, "init", "-b", "story/story-1")
    git(workspace, "config", "user.email", "product@example.com")
    git(workspace, "config", "user.name", "Product")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-m", "scaffold")
    # `services/scaffolder/src/scaffold.py` leaves this in the workspace config, so the
    # product's hooks are armed for every commit a worker makes.
    git(workspace, "config", "core.hooksPath", ".githooks")
    return workspace


def make_wrapper(workspace: Path, home: Path, agent_type: str = "codex") -> WorkerWrapper:
    config = WorkerWrapperConfig(
        broker_url="http://worker-broker:8001",
        broker_token="x" * 43,
        worker_id="worker-1",
        agent_type=agent_type,
    )
    wrapper = WorkerWrapper(config=config, broker_client=MagicMock())
    return wrapper


def prepare_turn(wrapper: WorkerWrapper, workspace: Path, monkeypatch, home: Path) -> None:
    """Everything the wrapper does to a checkout before the agent starts."""
    monkeypatch.setattr("worker_wrapper.wrapper.WORKSPACE_DIR", str(workspace))
    monkeypatch.setattr("worker_wrapper.wrapper.TASK_MD_PATH", str(workspace / "TASK.md"))
    monkeypatch.setattr("worker_wrapper.wrapper.STORY_DIR", str(workspace / ".story"))
    monkeypatch.setattr(
        "worker_wrapper.wrapper.OLD_TASKS_DIR", str(workspace / ".story" / "old_tasks")
    )
    monkeypatch.setenv("HOME", str(home))
    with patch.object(wrapper, "_git_pull", new_callable=AsyncMock):
        asyncio.run(
            wrapper._prepare_workspace(
                {"prompt": "# Task\n\nAdd a health probe.", "story_md": "# Story"}
            )
        )
    wrapper._fix_venv_paths()
    wrapper._install_compose_proxy()


class TestAnEngineeringTurnLeavesOnlyTheProductChange:
    def test_turn_preparation_dirties_nothing_the_product_tracks(
        self, product, tmp_path, monkeypatch
    ):
        """AC1: `git status --porcelain` names no orchestrator-authored change."""
        wrapper = make_wrapper(product, tmp_path / "home")
        tracked = {
            name: (product / name).read_bytes() for name in ("Makefile", "AGENTS.md", ".gitignore")
        }

        prepare_turn(wrapper, product, monkeypatch, tmp_path / "home")
        # The manager injects the agent's instructions after the clone.
        (product / instruction_filename(AgentType.CODEX)).write_text("# developer role\n")
        # …and the agent writes its own notes during the turn.
        (product / WorkerWorkspace.PROGRESS).write_text("- [x] read the task\n")
        (product / WorkerWorkspace.REPORT).write_text("# Report\n")

        for name, before in tracked.items():
            assert (product / name).read_bytes() == before, f"{name} is the product's, not ours"
        assert git(product, "status", "--porcelain") == ""

    def test_a_real_commit_carries_only_the_product_change(self, product, tmp_path, monkeypatch):
        """AC1/AC6: through the product's own `git add -A` hook, end to end."""
        wrapper = make_wrapper(product, tmp_path / "home")
        prepare_turn(wrapper, product, monkeypatch, tmp_path / "home")
        (product / instruction_filename(AgentType.CODEX)).write_text("# developer role\n")
        (product / WorkerWorkspace.PROGRESS).write_text("- [x] read the task\n")

        # The agent's actual work, and then its report, which the wrapper collects and
        # archives into `.story/old_tasks/` exactly as a finished turn does.
        (product / "services" / "backend" / "probe.py").write_text(
            "def probe():\n    return True\n"
        )
        (product / WorkerWorkspace.REPORT).write_text("# Report\n\nAdded the probe.\n")
        report = wrapper._read_worker_report()
        wrapper._archive_task({"task_id": "task-1"}, report)

        git(product, "commit", "--allow-empty", "-m", "feat: add a health probe")

        committed = git(product, "show", "--name-only", "--format=", "HEAD").split()
        assert committed == ["services/backend/probe.py"]

    def test_a_product_edit_to_a_file_the_orchestrator_used_to_touch_is_published(
        self, product, tmp_path, monkeypatch
    ):
        """AC3: the Makefile and AGENTS.md are the product's, so its edits travel."""
        wrapper = make_wrapper(product, tmp_path / "home")
        prepare_turn(wrapper, product, monkeypatch, tmp_path / "home")

        with (product / "Makefile").open("a") as handle:
            handle.write("\nprobe:\n\t@echo ok\n")
        with (product / "AGENTS.md").open("a") as handle:
            handle.write("\n## Probes\n\nDocument every probe.\n")
        git(product, "commit", "--allow-empty", "-m", "chore: document the probe target")

        committed = git(product, "show", "--name-only", "--format=", "HEAD").split()
        assert sorted(committed) == ["AGENTS.md", "Makefile"]
        assert wrapper._injected_paths_in_commit(git(product, "rev-parse", "HEAD")) == ([], None)


class TestTheTwoSidesAgreeOnTheInjectedNames:
    def test_every_manager_instruction_path_is_one_the_wrapper_excludes(self):
        """AC5: one definition, so the injector and the guard cannot drift apart."""
        for agent_type, path in manager_instruction_paths().items():
            assert path.startswith("/workspace/")
            name = path[len("/workspace/") :]
            assert name in INJECTED_PATHS
            if agent_type in (AgentType.CLAUDE, AgentType.CODEX):
                assert name == instruction_filename(agent_type)

    def test_the_kit_tracks_none_of_the_injected_names(self):
        """Nothing in the set may be a product file, or the guard would refuse its edit."""
        for path in INJECTED_PATHS:
            assert not (KIT / path.rstrip("/")).exists(), f"{path} belongs to the kit"


class TestThePublishGuard:
    """AC4: the last place a commit can be stopped is the seam that publishes it."""

    @pytest.fixture
    def published(self, product, tmp_path):
        """The product checkout with a bare origin it can actually push to."""
        remote = tmp_path / "origin.git"
        subprocess.run(  # noqa: S603
            ["git", "init", "--bare", "-b", "story/story-1", str(remote)],
            capture_output=True,
            check=True,
            timeout=60,
        )
        git(product, "remote", "add", "origin", str(remote))
        return remote

    def _wrapper_on(self, product, monkeypatch, tmp_path):
        monkeypatch.setattr("worker_wrapper.wrapper.WORKSPACE_DIR", str(product))
        return make_wrapper(product, tmp_path / "home")

    def test_a_clean_commit_is_pushed_and_read_back_unchanged(
        self, product, published, tmp_path, monkeypatch
    ):
        wrapper = self._wrapper_on(product, monkeypatch, tmp_path)
        (product / "services" / "backend" / "probe.py").write_text(
            "def probe():\n    return True\n"
        )
        git(product, "add", "-A")
        git(product, "-c", "core.hooksPath=/dev/null", "commit", "-m", "feat: probe")
        head = git(product, "rev-parse", "HEAD")

        result, error = wrapper._pushed_completed_result(
            WorkerCompletedResult(commit_sha=head[:7], content="Done"), "story/story-1"
        )

        assert error is None
        assert result.commit_sha == head
        assert git(published, "rev-parse", "story/story-1") == head

    def test_an_injected_path_in_the_commit_refuses_the_push_and_names_it(
        self, product, published, tmp_path, monkeypatch
    ):
        """The turn's own files reached the commit anyway: nothing is published."""
        wrapper = self._wrapper_on(product, monkeypatch, tmp_path)
        (product / "services" / "backend" / "probe.py").write_text(
            "def probe():\n    return True\n"
        )
        (product / WorkerWorkspace.PROGRESS).write_text("- [x] read the task\n")
        (product / ".story" / "old_tasks").mkdir(parents=True)
        (product / ".story" / "old_tasks" / "task-1.md").write_text("# archived\n")
        git(product, "add", "-Af")
        git(product, "-c", "core.hooksPath=/dev/null", "commit", "-m", "feat: probe")
        head = git(product, "rev-parse", "HEAD")

        result, error = wrapper._pushed_completed_result(
            WorkerCompletedResult(commit_sha=head, content="Done"), "story/story-1"
        )

        assert result is None
        assert WorkerWorkspace.PROGRESS in error
        assert ".story/old_tasks/task-1.md" in error
        assert "services/backend/probe.py" not in error
        readback = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--verify", "story/story-1"],
            cwd=str(published),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert readback.returncode != 0, "nothing may reach origin once the guard refuses"

    def test_the_refusal_keeps_the_workers_report(self, product, published, tmp_path, monkeypatch):
        """A refused completion is a failure that still carries its evidence."""
        wrapper = self._wrapper_on(product, monkeypatch, tmp_path)
        refused = wrapper._refused_completed_result(
            WorkerCompletedResult(
                commit_sha="a" * 40, content="summary", worker_report="# Report\n\nwhat happened"
            ),
            "Worker commit a carries orchestrator-injected paths: PROGRESS.md.",
        )

        assert refused.status == WorkerResultStatus.FAILED
        assert refused.worker_report == "# Report\n\nwhat happened"

    def test_a_root_commit_is_inspected_too(self, product, tmp_path, monkeypatch):
        """A first commit has no parent; its whole tree is what it adds."""
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        git(fresh, "init", "-b", "story/story-1")
        git(fresh, "config", "user.email", "p@example.com")
        git(fresh, "config", "user.name", "P")
        (fresh / WorkerWorkspace.TASK).write_text("# Task\n")
        (fresh / "app.py").write_text("x = 1\n")
        git(fresh, "add", "-Af")
        git(fresh, "commit", "-m", "root")
        wrapper = self._wrapper_on(fresh, monkeypatch, tmp_path)

        carried, error = wrapper._injected_paths_in_commit(git(fresh, "rev-parse", "HEAD"))

        assert error is None
        assert carried == [WorkerWorkspace.TASK]
