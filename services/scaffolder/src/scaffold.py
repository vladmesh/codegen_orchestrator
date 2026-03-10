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

    # Step 1: Clone repo
    log.info("scaffold_clone_start", repo=repo_full_name)
    clone_url = f"https://x-access-token:{github_token}@github.com/{repo_full_name}"
    rc, out, err = await _run_cmd(
        f'git clone "{clone_url}" .tmp-clone && '
        f"mv .tmp-clone/.git .git && "
        f"rm -rf .tmp-clone && "
        f'git config user.email "ai@codegen.local" && '
        f'git config user.name "Codegen Bot" && '
        f"git config core.hooksPath /dev/null",
        cwd=workspace,
    )
    result.commands_log.append(f"git clone: rc={rc}")
    if rc != 0:
        result.error = f"Git clone failed: {err}"
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
        "git push origin main",
        cwd=workspace,
    )
    result.commands_log.append(f"git push: rc={rc}")
    if rc != 0:
        result.error = f"Git push failed: {err or out}"
        log.error("scaffold_git_push_failed", error=err, stdout=out)
        return result

    # Step 6: Re-enable hooks and capture tree
    await _run_cmd("git config core.hooksPath .githooks", cwd=workspace)

    rc, tree_out, _ = await _run_cmd("tree -L 3 --noreport", cwd=workspace)
    if rc != 0:
        # tree command might not be installed; use find as fallback
        rc, tree_out, _ = await _run_cmd(
            "find . -maxdepth 3 -not -path './.git/*' -not -path './.venv/*' | sort",
            cwd=workspace,
        )

    result.success = True
    result.tree = tree_out.strip()
    log.info("scaffold_complete", tree_lines=len(result.tree.splitlines()))
    return result
