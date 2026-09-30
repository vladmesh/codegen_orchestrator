"""Two scaffold passes over one workspace — the re-delivery path.

The scaffold acks its message only at the very end, so a process death after a
successful push re-delivers the same message onto an already-scaffolded workspace.
This exercises that with a real local git: only copier, `make setup` and the remote
URL are substituted, so `git add`/`git commit`/`git push` behave exactly as they do
in the service, second pass included.
"""

import asyncio
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.scaffold import run_scaffold

REAL_EXEC = asyncio.create_subprocess_exec

# Byte-identical on both passes: that is what makes the second commit empty.
TEMPLATE_FILES = {
    "Makefile": "setup:\n\techo ok\n",
    "src/main.py": "print('hello')\n",
    ".copier-answers.yml": "_commit: 0.3.0\n_src_path: /data/service-template\n",
}


def _fake_proc(rc: int, stdout: bytes = b"", stderr: bytes = b""):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = rc
    return proc


@pytest.fixture
def local_remote(tmp_path):
    """A bare repo standing in for GitHub — no network, real git wire protocol.

    Seeded like `create_repo(auto_init=True)`: one initial commit on `main`.
    """
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
    seed = tmp_path / "seed"
    git = ["git", "-C", str(seed), "-c", "user.email=s@x", "-c", "user.name=seed"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "README.md").write_text("# my-project\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "Initial commit"], check=True)
    subprocess.run([*git, "push", "-q", str(remote), "main"], check=True)
    return remote


@pytest.fixture
def settings(tmp_path):
    mock = MagicMock()
    mock.workspace_base_path = str(tmp_path / "workspaces")
    return mock


def _exec_shim(local_remote, workspace, copier_calls):
    """Substitute copier, make and the remote URL; run every git command for real."""

    async def fake_exec(*args, **kwargs):
        cmd = tuple(args)
        if cmd[:2] == ("copier", "copy"):
            copier_calls.append(cmd)
            for relative, content in TEMPLATE_FILES.items():
                target = workspace / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            return _fake_proc(0)
        if cmd[0] == "make":
            return _fake_proc(0)
        if cmd[:3] == ("git", "remote", "add"):
            cmd = (*cmd[:4], str(local_remote))
        return await REAL_EXEC(*cmd, **kwargs)

    return fake_exec


async def _scaffold_once(settings, local_remote, workspace, copier_calls):
    with patch(
        "src.scaffold.asyncio.create_subprocess_exec",
        side_effect=_exec_shim(local_remote, workspace, copier_calls),
    ):
        return await run_scaffold(
            project_id="proj-123",
            repository_id="repo-456",
            template_repo="/data/service-template",
            template_ref="0.3.0",
            project_name="my-project",
            modules="backend",
            task_description="Build a bot",
            repo_full_name="org/my-project",
            github_token="ghs_fake_token",  # noqa: S106
            settings=settings,
        )


@pytest.mark.asyncio
async def test_second_scaffold_pass_over_same_workspace_succeeds(settings, local_remote, tmp_path):
    workspace = tmp_path / "workspaces" / "repo-456"
    copier_calls = []

    first = await _scaffold_once(settings, local_remote, workspace, copier_calls)
    assert first.success is True, first.error
    assert "git commit: rc=0 (committed)" in first.commands_log

    second = await _scaffold_once(settings, local_remote, workspace, copier_calls)

    assert second.success is True, second.error
    assert second.error is None
    assert len(copier_calls) == 2
    # The second pass had nothing new to record, and said so.
    assert any("nothing to commit" in entry for entry in second.commands_log)
    assert "git push: rc=0" in second.commands_log

    # And the project survived: the auto_init commit plus one scaffold commit on the
    # remote, same tree as after pass one.
    log = subprocess.run(
        ["git", "--git-dir", str(local_remote), "log", "--oneline", "main"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert len(log.stdout.strip().splitlines()) == 2
    assert (workspace / "src" / "main.py").read_text() == TEMPLATE_FILES["src/main.py"]


GITHUB_NOT_FOUND = (
    b"remote: Repository not found.\n"
    b"fatal: repository 'https://github.com/org/my-project/' not found\n"
)


@pytest.mark.asyncio
async def test_scaffold_waits_until_new_repository_is_served(settings, local_remote, tmp_path):
    """The 2026-09-24 incident: the git endpoint 404s a repository the API just created.

    The first fetches get GitHub's "Repository not found"; the scaffold backs off and
    builds on the auto_init commit once the remote serves it, instead of failing.
    """
    workspace = tmp_path / "workspaces" / "repo-456"
    copier_calls = []
    real_shim = _exec_shim(local_remote, workspace, copier_calls)
    fetches = []

    async def lagging_github(*args, **kwargs):
        if tuple(args[:2]) == ("git", "fetch"):
            fetches.append(args)
            if len(fetches) <= 2:
                return _fake_proc(128, stderr=GITHUB_NOT_FOUND)
        return await real_shim(*args, **kwargs)

    sleep = AsyncMock()
    with (
        patch("src.scaffold.asyncio.create_subprocess_exec", side_effect=lagging_github),
        patch("src.scaffold.asyncio.sleep", sleep),
    ):
        result = await run_scaffold(
            project_id="proj-123",
            repository_id="repo-456",
            template_repo="/data/service-template",
            template_ref="0.3.0",
            project_name="my-project",
            modules="backend",
            task_description="Build a bot",
            repo_full_name="org/my-project",
            github_token="ghs_fake_token",  # noqa: S106
            settings=settings,
        )

    assert result.success is True, result.error
    assert len(fetches) == 3
    assert [c.args[0] for c in sleep.await_args_list] == [1, 2]
    assert "git init+fetch: rc=0 attempts=3" in result.commands_log
    # The scaffold commit sits on top of GitHub's initial commit: nothing was force-pushed.
    log = subprocess.run(
        ["git", "--git-dir", str(local_remote), "log", "--format=%s", "main"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert log.stdout.splitlines()[-1] == "Initial commit"
    assert len(log.stdout.splitlines()) == 2
