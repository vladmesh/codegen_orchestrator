"""Git operations for worker containers (clone, branch, token refresh)."""

import base64

import structlog

from .docker_ops import DockerClientWrapper

logger = structlog.get_logger()

# Empty hooks dir outside /workspace, so project hooks never gate worker git operations
# and the directory itself cannot be committed. See setup_git_repo.
WORKER_HOOKS_DIR = "/tmp/worker-githooks"  # noqa: S108


async def checkout_branch(docker: DockerClientWrapper, container_id: str, branch: str, worker_id: str) -> bool:
    """Checkout a story branch in the workspace.

    Creates the branch from current HEAD if it doesn't exist,
    or switches to it if it already exists (e.g. subsequent tasks in same story).
    Also sets up tracking so `git push` works without specifying remote/branch.
    """
    logger.info("checkout_branch_start", worker_id=worker_id, branch=branch)
    script = (
        f"cd /workspace && "
        f"git fetch origin {branch} 2>/dev/null || true && "
        f"git checkout -b {branch} 2>/dev/null || git checkout {branch} && "
        f"git push -u origin {branch} 2>/dev/null || true"
    )
    encoded = base64.b64encode(script.encode()).decode()
    cmd = f"bash -c 'echo {encoded} | base64 -d | bash'"
    exit_code, output = await docker.exec_in_container(container_id, cmd, timeout=30)
    if exit_code != 0:
        logger.error(
            "checkout_branch_failed",
            worker_id=worker_id,
            branch=branch,
            error=output,
        )
        return False
    logger.info("checkout_branch_complete", worker_id=worker_id, branch=branch)
    return True


async def refresh_git_token(
    docker: DockerClientWrapper, container_id: str, repo: str, token: str, worker_id: str
) -> bool:
    """Update git remote URL with fresh token in existing workspace."""
    script = f"cd /workspace && git remote set-url origin 'https://x-access-token:{token}@github.com/{repo}'"
    encoded = base64.b64encode(script.encode()).decode()
    cmd = f"bash -c 'echo {encoded} | base64 -d | bash'"
    exit_code, output = await docker.exec_in_container(container_id, cmd, timeout=30)
    if exit_code != 0:
        logger.error("git_token_refresh_failed", worker_id=worker_id, error=output)
        return False
    logger.info("git_token_refreshed", worker_id=worker_id, repo=repo)
    return True


async def setup_git_repo(
    docker: DockerClientWrapper,
    container_id: str,
    repo: str,
    token: str,
    worker_id: str,
) -> bool:
    """Clone repository and configure git before LLM starts.

    This saves tokens by automating:
    - git clone
    - git user config
    - hooksPath pointed away from the project's .githooks

    Generated projects ship .githooks/pre-push that runs `make lint` when Docker is
    absent, which resolves to .venv/bin/ruff — neither exists in a worker container, so
    the hook exits 127 under `set -euo pipefail` and every push carrying real file
    changes is rejected. The agent's commits then never leave the container while it
    still reports success. Worker pushes are gated by the PR CI run instead.

    Returns:
        True if setup succeeded, False otherwise
    """
    logger.info("git_repo_setup_start", worker_id=worker_id, repo=repo)

    setup_script = f"""set -e
cd /workspace
git clone "https://x-access-token:{token}@github.com/{repo}" .
mkdir -p {WORKER_HOOKS_DIR}
git config core.hooksPath {WORKER_HOOKS_DIR}
git config user.name "AI Agent"
git config user.email "ai@codegen.local"
"""

    encoded = base64.b64encode(setup_script.encode()).decode()
    cmd = f"bash -c 'echo {encoded} | base64 -d | bash'"

    exit_code, output = await docker.exec_in_container(
        container_id,
        cmd,
        timeout=120,
    )

    if exit_code != 0:
        logger.error(
            "git_repo_setup_failed",
            worker_id=worker_id,
            repo=repo,
            exit_code=exit_code,
            error=output,
        )
        return False

    logger.info("git_repo_setup_complete", worker_id=worker_id, repo=repo)
    return True
