"""Git operations for worker containers (clone, branch, token refresh)."""

import base64

import structlog

from .docker_ops import DockerClientWrapper

logger = structlog.get_logger()


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
