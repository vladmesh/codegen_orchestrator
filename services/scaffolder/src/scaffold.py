"""Core scaffold logic — copier + make setup + git push.

Pure business logic with no queue/API dependencies.
All I/O happens via asyncio subprocess calls.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class ScaffoldResult:
    """Result of a scaffold operation."""

    success: bool
    skipped: bool = False
    tree: str = ""
    error: str | None = None
    commands_log: list[str] = field(default_factory=list)


async def _run_cmd(cmd: str, cwd: Path | None = None, timeout: int = 600) -> tuple[int, str, str]:
    """Run a shell command and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode, stdout.decode(), stderr.decode()


async def run_scaffold(
    *,
    project_id: str,
    repository_id: str,
    template_repo: str,
    project_name: str,
    modules: str,
    task_description: str,
    repo_full_name: str,
    github_token: str,
    settings,
) -> ScaffoldResult:
    """Run the full scaffold pipeline.

    1. Create workspace directory
    2. Clone repo (or init git)
    3. Run copier copy
    4. Run make setup
    5. Git add, commit, push
    6. Capture tree output

    Args:
        project_id: Project ID for logging
        repository_id: Repository ID (used as workspace directory name)
        template_repo: Path to service-template on disk
        project_name: Sanitized project name for copier
        modules: Comma-separated module list
        task_description: Description passed to copier as data
        repo_full_name: GitHub repo in "owner/repo" format
        github_token: GitHub token for git operations
        settings: Service settings (workspace_base_path, etc.)

    Returns:
        ScaffoldResult with success status, tree output, and error details.
    """
    log = logger.bind(project_id=project_id, repository_id=repository_id)
    workspace = Path(settings.workspace_base_path) / repository_id
    workspace.mkdir(parents=True, exist_ok=True)

    result = ScaffoldResult(success=False)

    # Step 1: Init git and fetch from remote (in-place, no temp dirs)
    log.info("scaffold_clone_start", repo=repo_full_name)
    clone_url = f"https://x-access-token:{github_token}@github.com/{repo_full_name}"
    rc, out, err = await _run_cmd(
        f"git init && "
        f'git remote add origin "{clone_url}" || true && '
        f"git fetch origin && "
        # If remote has main, start from it; otherwise create fresh branch
        f"(git reset --soft origin/main 2>/dev/null || true) && "
        f"git checkout -B main && "
        f'git config user.email "ai@codegen.local" && '
        f'git config user.name "Codegen Bot" && '
        f"git config core.hooksPath /dev/null",
        cwd=workspace,
    )
    result.commands_log.append(f"git init+fetch: rc={rc}")
    if rc != 0:
        result.error = f"Git init/fetch failed: {err}"
        log.error("scaffold_clone_failed", error=err)
        return result

    # Step 2: Write copier data file (task_description via YAML to avoid shell escaping)
    data_file = workspace / "_copier_data.yml"
    desc_text = task_description or ""
    indented = "\n".join(f"  {line}" for line in desc_text.splitlines()) if desc_text else '  ""'
    data_file.write_text(f"task_description: |\n{indented}\n")

    # Step 3: Run copier
    log.info("scaffold_copier_start", template=template_repo, modules=modules)
    rc, out, err = await _run_cmd(
        f"copier copy {template_repo} {workspace} "
        f'--data "project_name={project_name}" '
        f'--data "modules={modules}" '
        f"--data-file {data_file} "
        f"--trust --defaults --overwrite --vcs-ref=HEAD",
        cwd=workspace,
    )
    result.commands_log.append(f"copier copy: rc={rc}")
    if rc != 0:
        result.error = f"Copier failed: {err or out}"
        log.error("scaffold_copier_failed", error=err, stdout=out)
        return result

    # Clean up data file
    data_file.unlink(missing_ok=True)

    # Step 4: Run make setup
    log.info("scaffold_make_setup_start")
    rc, out, err = await _run_cmd("make setup", cwd=workspace)
    result.commands_log.append(f"make setup: rc={rc}")
    if rc != 0:
        result.error = f"make setup failed: {err or out}"
        log.error("scaffold_make_setup_failed", error=err, stdout=out)
        return result

    # Step 5: Git add, commit, push
    # Re-disable hooks before push (make setup may re-enable them via `git config core.hooksPath`)
    log.info("scaffold_git_push_start")
    rc, out, err = await _run_cmd(
        "git config core.hooksPath /dev/null && "
        "git add . && "
        f'git commit -m "feat: scaffold {project_name} with modules: {modules}" && '
        "git push -u origin main",
        cwd=workspace,
    )
    result.commands_log.append(f"git push: rc={rc}")
    if rc != 0:
        result.error = f"Git push failed: {err or out}"
        log.error("scaffold_git_push_failed", error=err, stdout=out)
        return result

    # Step 6: Re-enable hooks and capture tree
    await _run_cmd("git config core.hooksPath .githooks", cwd=workspace)

    result.success = True
    result.tree = await _capture_tree(workspace)
    log.info("scaffold_complete", tree_lines=len(result.tree.splitlines()))
    return result


async def _capture_tree(workspace: Path) -> str:
    """Capture directory tree output for a workspace."""
    rc, tree_out, _ = await _run_cmd("tree -L 3 --noreport", cwd=workspace)
    if rc != 0:
        rc, tree_out, _ = await _run_cmd(
            "find . -maxdepth 3 -not -path './.git/*' -not -path './.venv/*' | sort",
            cwd=workspace,
        )
    return tree_out.strip()


def _workspace_has_files(workspace: Path) -> bool:
    """Check if workspace dir exists and has files beyond .git."""
    if not workspace.is_dir():
        return False
    for entry in workspace.iterdir():
        if entry.name != ".git":
            return True
    return False


async def run_ensure_workspace(
    *,
    repository_id: str,
    project_name: str,
    repo_full_name: str,
    github_token: str,
    settings,
    repo_exists_on_github: bool,
) -> ScaffoldResult:
    """Ensure a workspace exists on disk. Three outcomes:

    1. Workspace already has files → skip (return success + skipped=True)
    2. Workspace missing, repo exists on GitHub → git clone + make setup
    3. Workspace missing, repo doesn't exist → error (caller should use full scaffold)

    Args:
        repository_id: Repository ID (workspace directory name).
        project_name: Project name for logging.
        repo_full_name: GitHub repo in "owner/repo" format.
        github_token: GitHub token for git operations.
        settings: Service settings (workspace_base_path).
        repo_exists_on_github: Whether the repo exists on GitHub.

    Returns:
        ScaffoldResult with success/skipped status.
    """
    log = logger.bind(repository_id=repository_id, project_name=project_name)
    workspace = Path(settings.workspace_base_path) / repository_id
    result = ScaffoldResult(success=False)

    # Case 1: workspace already has files → skip
    if _workspace_has_files(workspace):
        log.info("ensure_workspace_skip", reason="workspace_exists")
        result.success = True
        result.skipped = True
        return result

    # Case 3: no workspace and no repo on GitHub → can't recover
    if not repo_exists_on_github:
        result.error = (
            f"Workspace missing and repo {repo_full_name} not found on GitHub. "
            "Full scaffold required but project is not in draft."
        )
        log.error("ensure_workspace_no_repo", error=result.error)
        return result

    # Case 2: workspace missing, repo exists → git clone + make setup
    log.info("ensure_workspace_clone_start", repo=repo_full_name)
    workspace.mkdir(parents=True, exist_ok=True)

    clone_url = f"https://x-access-token:{github_token}@github.com/{repo_full_name}"
    rc, out, err = await _run_cmd(
        f'git clone "{clone_url}" . && '
        f'git config user.email "ai@codegen.local" && '
        f'git config user.name "Codegen Bot" && '
        f"git config core.hooksPath /dev/null",
        cwd=workspace,
    )
    result.commands_log.append(f"git clone: rc={rc}")
    if rc != 0:
        result.error = f"Git clone failed: {err}"
        log.error("ensure_workspace_clone_failed", error=err)
        return result

    # Run make setup (installs deps, enables hooks)
    log.info("ensure_workspace_make_setup")
    rc, out, err = await _run_cmd("make setup", cwd=workspace)
    result.commands_log.append(f"make setup: rc={rc}")
    if rc != 0:
        result.error = f"make setup failed: {err or out}"
        log.error("ensure_workspace_setup_failed", error=err, stdout=out)
        return result

    # Re-enable hooks
    await _run_cmd("git config core.hooksPath .githooks", cwd=workspace)

    result.success = True
    result.tree = await _capture_tree(workspace)
    log.info("ensure_workspace_complete", tree_lines=len(result.tree.splitlines()))
    return result
