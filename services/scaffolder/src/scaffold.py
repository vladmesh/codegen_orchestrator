"""Core scaffold logic — copier + make setup + git push.

Pure business logic with no queue/API dependencies.
All I/O happens via asyncio subprocess calls.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import os
from pathlib import Path

import structlog
import yaml

from shared.diagnostics import redact_diagnostic
from src.validation import ScaffoldInputError, validate_modules, validate_project_name

logger = structlog.get_logger(__name__)


@dataclass
class ScaffoldResult:
    """Result of a scaffold operation."""

    success: bool
    skipped: bool = False
    tree: str = ""
    error: str | None = None
    commands_log: list[str] = field(default_factory=list)
    template_commit: str | None = None


async def _run_cmd(
    args: list[str],
    cwd: Path | None = None,
    timeout: int = 600,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run an argument vector and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode, stdout.decode(), stderr.decode()


def _workspace_path(workspace_root: str, repository_id: str) -> Path:
    """Return the owned workspace path or reject traversal and symlink escapes."""
    root = Path(workspace_root).resolve()
    candidate = Path(repository_id)
    if not repository_id or candidate.name != repository_id:
        raise ScaffoldInputError("repository_id must be a single workspace directory name")
    workspace = (root / candidate).resolve()
    if workspace.parent != root:
        raise ScaffoldInputError("workspace is outside the configured workspace root")
    return workspace


def _git_auth_env(token: str) -> dict[str, str]:
    """Provide GitHub auth via Git configuration, never a credentialed URL."""
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {encoded}",
        }
    )
    return env


def _failure_detail(stderr: str, stdout: str, token: str) -> str:
    return redact_diagnostic(stderr or stdout, secrets=(token,))


async def run_scaffold(  # noqa: PLR0915
    *,
    project_id: str,
    repository_id: str,
    template_repo: str,
    template_ref: str,
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
        template_repo: GitHub service-template source
        template_ref: Immutable release tag or commit
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

    # Validate before anything reaches the shell: project_name and modules are
    # interpolated into the copier and git-commit commands below.
    try:
        validate_project_name(project_name)
        validate_modules(modules)
    except ScaffoldInputError as e:
        log.error("scaffold_invalid_input", error=str(e))
        return ScaffoldResult(success=False, error=str(e))

    try:
        workspace = _workspace_path(settings.workspace_base_path, repository_id)
    except ScaffoldInputError as exc:
        log.error("scaffold_invalid_workspace", error=str(exc))
        return ScaffoldResult(success=False, error=str(exc))
    workspace.mkdir(parents=True, exist_ok=True)

    result = ScaffoldResult(success=False)

    # Step 1: Init git and fetch from remote (in-place, no temp dirs)
    log.info("scaffold_clone_start", repo=repo_full_name)
    clone_url = f"https://github.com/{repo_full_name}"
    rc, out, err = await _run_cmd(["git", "init"], cwd=workspace)
    if rc == 0:
        rc, out, err = await _run_cmd(["git", "remote", "add", "origin", clone_url], cwd=workspace)
    if rc != 0 and "remote origin already exists" not in err:
        detail = _failure_detail(err, out, github_token)
        result.error = f"Git init/fetch failed: {detail}"
        log.error("scaffold_clone_failed", error=detail)
        return result
    rc, out, err = await _run_cmd(
        ["git", "fetch", "origin"], cwd=workspace, env=_git_auth_env(github_token)
    )
    result.commands_log.append(f"git init+fetch: rc={rc}")
    if rc != 0:
        detail = _failure_detail(err, out, github_token)
        result.error = f"Git init/fetch failed: {detail}"
        log.error("scaffold_clone_failed", error=detail)
        return result
    await _run_cmd(["git", "reset", "--soft", "origin/main"], cwd=workspace)
    for args in (
        ["git", "checkout", "-B", "main"],
        ["git", "config", "user.email", "ai@codegen.local"],
        ["git", "config", "user.name", "Codegen Bot"],
        ["git", "config", "core.hooksPath", "/dev/null"],
    ):
        rc, out, err = await _run_cmd(args, cwd=workspace)
        if rc != 0:
            detail = _failure_detail(err, out, github_token)
            result.error = f"Git init/fetch failed: {detail}"
            log.error("scaffold_clone_failed", error=detail)
            return result

    # Step 2: Write copier data file (task_description via YAML to avoid shell escaping)
    data_file = workspace / "_copier_data.yml"
    desc_text = task_description or ""
    indented = "\n".join(f"  {line}" for line in desc_text.splitlines()) if desc_text else '  ""'
    data_file.write_text(f"task_description: |\n{indented}\n")

    # Step 3: Run copier
    log.info(
        "scaffold_copier_start", template=template_repo, template_ref=template_ref, modules=modules
    )
    rc, out, err = await _run_cmd(
        [
            "copier",
            "copy",
            template_repo,
            str(workspace),
            "--data",
            f"project_name={project_name}",
            "--data",
            f"modules={modules}",
            "--data-file",
            str(data_file),
            "--defaults",
            "--overwrite",
            f"--vcs-ref={template_ref}",
        ],
        cwd=workspace,
    )
    result.commands_log.append(f"copier copy: rc={rc}")
    if rc != 0:
        detail = _failure_detail(err, out, github_token)
        result.error = f"Copier failed: {detail}"
        log.error("scaffold_copier_failed", error=detail)
        return result

    answers_path = workspace / ".copier-answers.yml"
    try:
        answers = yaml.safe_load(answers_path.read_text()) or {}
        result.template_commit = str(answers["_commit"])
    except (OSError, KeyError, TypeError, yaml.YAMLError) as exc:
        result.error = f"Copier did not record resolved template commit: {exc}"
        log.error("scaffold_template_commit_missing", error=str(exc))
        return result
    log.info(
        "scaffold_template_resolved",
        template=template_repo,
        requested_ref=template_ref,
        template_commit=result.template_commit,
    )

    # Clean up data file
    data_file.unlink(missing_ok=True)

    # Step 4: Run make setup
    log.info("scaffold_make_setup_start")
    rc, out, err = await _run_cmd(["make", "setup"], cwd=workspace)
    result.commands_log.append(f"make setup: rc={rc}")
    if rc != 0:
        detail = _failure_detail(err, out, github_token)
        result.error = f"make setup failed: {detail}"
        log.error("scaffold_make_setup_failed", error=detail)
        return result

    # Step 5: Git add, commit, push
    # Re-disable hooks before push (make setup may re-enable them via `git config core.hooksPath`)
    log.info("scaffold_git_push_start")
    for args in (
        ["git", "config", "core.hooksPath", "/dev/null"],
        ["git", "add", "."],
        ["git", "commit", "-m", f"feat: scaffold {project_name} with modules: {modules}"],
        ["git", "push", "-u", "origin", "main"],
    ):
        rc, out, err = await _run_cmd(args, cwd=workspace, env=_git_auth_env(github_token))
        if rc != 0:
            break
    result.commands_log.append(f"git push: rc={rc}")
    if rc != 0:
        detail = _failure_detail(err, out, github_token)
        result.error = f"Git push failed: {detail}"
        log.error("scaffold_git_push_failed", error=detail)
        return result

    # Step 6: Re-enable hooks and capture tree
    await _run_cmd(["git", "config", "core.hooksPath", ".githooks"], cwd=workspace)

    result.success = True
    result.tree = await _capture_tree(workspace)
    log.info("scaffold_complete", tree_lines=len(result.tree.splitlines()))
    return result


async def _capture_tree(workspace: Path) -> str:
    """Capture directory tree output for a workspace."""
    try:
        rc, tree_out, _ = await _run_cmd(
            [
                "tree",
                "-L",
                "3",
                "--noreport",
                "-I",
                ".venv|node_modules|.git|__pycache__|.mypy_cache|.ruff_cache",
            ],
            cwd=workspace,
        )
    except FileNotFoundError:
        rc = 1
        tree_out = ""
    if rc != 0:
        rc, tree_out, _ = await _run_cmd(
            [
                "find",
                ".",
                "-maxdepth",
                "3",
                "-not",
                "-path",
                "./.git/*",
                "-not",
                "-path",
                "./.venv/*",
            ],
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

    # project_name is interpolated into the git clone target and commands below.
    try:
        validate_project_name(project_name)
    except ScaffoldInputError as e:
        log.error("ensure_workspace_invalid_input", error=str(e))
        return ScaffoldResult(success=False, error=str(e))

    try:
        workspace = _workspace_path(settings.workspace_base_path, repository_id)
    except ScaffoldInputError as exc:
        log.error("ensure_workspace_invalid_workspace", error=str(exc))
        return ScaffoldResult(success=False, error=str(exc))
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

    clone_url = f"https://github.com/{repo_full_name}"
    rc, out, err = await _run_cmd(
        ["git", "clone", clone_url, "."],
        cwd=workspace,
        env=_git_auth_env(github_token),
    )
    result.commands_log.append(f"git clone: rc={rc}")
    if rc != 0:
        detail = _failure_detail(err, out, github_token)
        result.error = f"Git clone failed: {detail}"
        log.error("ensure_workspace_clone_failed", error=detail)
        return result
    for args in (
        ["git", "config", "user.email", "ai@codegen.local"],
        ["git", "config", "user.name", "Codegen Bot"],
        ["git", "config", "core.hooksPath", "/dev/null"],
    ):
        rc, out, err = await _run_cmd(args, cwd=workspace)
        if rc != 0:
            detail = _failure_detail(err, out, github_token)
            result.error = f"Git clone failed: {detail}"
            log.error("ensure_workspace_clone_failed", error=detail)
            return result

    # Run make setup (installs deps, enables hooks)
    log.info("ensure_workspace_make_setup")
    rc, out, err = await _run_cmd(["make", "setup"], cwd=workspace)
    result.commands_log.append(f"make setup: rc={rc}")
    if rc != 0:
        detail = _failure_detail(err, out, github_token)
        result.error = f"make setup failed: {detail}"
        log.error("ensure_workspace_setup_failed", error=detail)
        return result

    # Re-enable hooks
    await _run_cmd(["git", "config", "core.hooksPath", ".githooks"], cwd=workspace)

    result.success = True
    result.tree = await _capture_tree(workspace)
    log.info("ensure_workspace_complete", tree_lines=len(result.tree.splitlines()))
    return result
