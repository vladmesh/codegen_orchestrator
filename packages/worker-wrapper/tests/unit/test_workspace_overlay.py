import subprocess

import pytest
from worker_wrapper.workspace_overlay import WorkspaceOverlay, WorkspaceOverlayError


def _git(root, *args, check=True):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=check)


@pytest.fixture
def product(tmp_path):
    remote = tmp_path.parent / f"{tmp_path.name}-remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare")
    _git(tmp_path, "init", "-b", "story/test")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / "AGENTS.md").write_text("# Product instructions\n")
    (tmp_path / "CLAUDE.md").write_text("# Product Claude instructions\n")
    (tmp_path / "Makefile").write_text("worker-start:\n\t@docker compose up\n")
    (tmp_path / "product.py").write_text("VALUE = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "initial")
    _git(tmp_path, "remote", "add", "origin", str(remote))
    _git(tmp_path, "push", "-u", "origin", "story/test")
    return tmp_path


@pytest.mark.parametrize("instruction_name", ["AGENTS.md", "CLAUDE.md"])
def test_runtime_overlay_is_visible_but_sanitized_commit_keeps_product_tree(
    product, instruction_name
):
    overlay = WorkspaceOverlay(product)
    original_instruction = (product / instruction_name).read_text()
    original_makefile = (product / "Makefile").read_text()

    overlay.configure_instruction(instruction_name, "# Dynamic worker instructions\n")
    overlay.activate(task="Fix the product\n", story="Story context\n")
    (product / "PROGRESS.md").write_text("working\n")
    (product / ".venv_paths_fixed").touch()
    (product / ".shebangs_fixed").touch()
    (product / ".story" / "old_tasks").mkdir(parents=True)
    (product / ".story" / "old_tasks" / "old.md").write_text("old context\n")

    assert "Dynamic worker instructions" in (product / instruction_name).read_text()
    assert "orchestrator overrides" in (product / "Makefile").read_text()
    assert (product / "TASK.md").read_text() == "Fix the product\n"
    assert (product / ".story" / "STORY.md").read_text() == "Story context\n"

    (product / "product.py").write_text("VALUE = 2\n")
    with (product / "Makefile").open("a") as stream:
        stream.write("\nproduct-check:\n\t@echo checked\n")
    _git(product, "update-index", "--no-skip-worktree", "--", instruction_name)
    _git(product, "update-index", "--no-skip-worktree", "--", "Makefile")
    _git(product, "add", "-A")
    _git(product, "commit", "-m", "agent change")

    sanitized = overlay.sanitize_unpublished_commits(
        "story/test", _git(product, "rev-parse", "HEAD").stdout.strip()
    )

    assert sanitized == _git(product, "rev-parse", "HEAD").stdout.strip()
    tree = set(_git(product, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines())
    assert "product.py" in tree
    assert (
        not {
            "TASK.md",
            ".story/STORY.md",
            ".story/old_tasks/old.md",
            "PROGRESS.md",
            ".venv_paths_fixed",
            ".shebangs_fixed",
        }
        & tree
    )
    assert _git(product, "show", f"HEAD:{instruction_name}").stdout == original_instruction
    committed_makefile = _git(product, "show", "HEAD:Makefile").stdout
    assert committed_makefile.startswith(original_makefile)
    assert "product-check:" in committed_makefile
    assert "orchestrator overrides" not in committed_makefile
    assert _git(product, "status", "--porcelain").stdout == ""


def test_second_turn_recovers_an_interrupted_overlay_without_duplication(product):
    overlay = WorkspaceOverlay(product)
    overlay.configure_instruction("AGENTS.md", "dynamic\n")
    overlay.activate(task="first\n", story="first story\n")
    (product / ".story" / "old_tasks").mkdir(parents=True)
    (product / ".story" / "old_tasks" / "old.md").write_text("old context\n")
    (product / "product.py").write_text("VALUE = 2\n")
    _git(product, "add", "-A")
    _git(product, "commit", "-m", "interrupted polluted commit")

    head_before = _git(product, "rev-parse", "HEAD").stdout.strip()
    overlay.recover()
    overlay.activate(task="second\n", story="second story\n")

    assert _git(product, "rev-parse", "HEAD").stdout.strip() == head_before
    assert (product / "AGENTS.md").read_text().count("dynamic") == 1
    assert (product / "Makefile").read_text().count("# --- orchestrator overrides ---") == 1
    assert (product / "TASK.md").read_text() == "second\n"
    assert (product / ".story" / "old_tasks" / "old.md").read_text() == "old context\n"


def test_cleanup_failure_is_explicit(product, monkeypatch):
    overlay = WorkspaceOverlay(product)
    overlay.configure_instruction("AGENTS.md", "dynamic\n")
    overlay.activate(task="task\n", story=None)
    (product / "product.py").write_text("VALUE = 2\n")
    _git(product, "add", "-A")
    _git(product, "commit", "-m", "agent change")

    def fail_sanitation(*_args, **_kwargs):
        raise WorkspaceOverlayError("disk full")

    monkeypatch.setattr(overlay, "_sanitized_tree", fail_sanitation)

    with pytest.raises(WorkspaceOverlayError, match="disk full"):
        overlay.sanitize_unpublished_commits(
            "story/test", _git(product, "rev-parse", "HEAD").stdout.strip()
        )


def test_failed_turn_deactivation_preserves_head_index_wip_and_context(product, monkeypatch):
    overlay = WorkspaceOverlay(product)
    overlay.configure_instruction("AGENTS.md", "dynamic\n")
    overlay.activate(task="retry me\n", story="persistent story\n")
    (product / ".story" / "old_tasks").mkdir(parents=True)
    (product / ".story" / "old_tasks" / "prior.md").write_text("prior task\n")
    (product / "PROGRESS.md").write_text("unfinished plan\n")
    (product / "product.py").write_text("VALUE = 2\n")
    _git(product, "add", "-A")
    head_before = _git(product, "rev-parse", "HEAD").stdout.strip()
    staged_before = _git(product, "diff", "--cached", "--binary").stdout
    monkeypatch.setattr("worker_wrapper.wrapper.WORKSPACE_DIR", str(product))

    from worker_wrapper.wrapper import WorkerWrapper

    WorkerWrapper._restore_workspace_after_turn()

    assert _git(product, "rev-parse", "HEAD").stdout.strip() == head_before
    assert _git(product, "diff", "--cached", "--binary").stdout == staged_before
    assert (product / "TASK.md").read_text() == "retry me\n"
    assert (product / ".story" / "STORY.md").read_text() == "persistent story\n"
    assert (product / ".story" / "old_tasks" / "prior.md").read_text() == "prior task\n"
    assert (product / "PROGRESS.md").read_text() == "unfinished plan\n"
    assert not (product / ".git" / "MERGE_HEAD").exists()

    overlay.recover()
    overlay.activate(task=None, story=None)

    assert _git(product, "rev-parse", "HEAD").stdout.strip() == head_before
    assert _git(product, "diff", "--cached", "--binary").stdout == staged_before
    assert (product / "TASK.md").read_text() == "retry me\n"
    assert (product / ".story" / "STORY.md").read_text() == "persistent story\n"
    assert not (product / ".git" / "MERGE_HEAD").exists()
