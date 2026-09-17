"""Git operations for worker containers (clone, branch, token refresh).

Every git command here is the *manager's* infrastructure git, not the developer
agent's. The developer workspace is reused between the stories of one project,
and the first story's `make setup` leaves `core.hooksPath=.githooks` behind in
it, so a plain `git push` from this side runs the generated product's `pre-push`
hook: lint and tests for every service the hook believes changed, far past the
exec timeout, and the story never starts.

The fix is the one `services/scaffolder/src/scaffold.py` already uses for its own
infrastructure git — hooks pointed at `/dev/null` — but per command, never as a
write to the workspace's git config: the product's hooks must keep running for
the developer agent's own commits and pushes.
"""

import base64

import structlog

from .docker_ops import DockerClientWrapper

logger = structlog.get_logger()

# Prefix for every git invocation the manager makes inside a worker workspace.
# `-c` configures this process only, so the workspace's own `core.hooksPath`
# (the product's `.githooks`) is left exactly as the product installed it.
GIT = "git -c core.hooksPath=/dev/null"


def build_checkout_script(branch: str) -> str:
    """The shell the manager runs to put a workspace on `branch`.

    - The default branch is read from the repository (`origin/HEAD`), not
      hard-coded here: the scaffolder is what publishes it.
    - A story branch that does not exist yet is cut from the *freshly fetched*
      default branch, never from whatever the reused workspace had checked out.
    - A story branch that already exists resumes at its remote tip, and is never
      reset or rebased onto the default branch: a local tip that is ahead of the
      remote is the developer's own work and is kept.
    - The push that establishes the upstream is not swallowed, and the script
      ends by reading the upstream back, so a checkout that did not do its job
      exits non-zero.
    """
    return f"""set -e
cd /workspace
{GIT} remote set-head origin --auto >/dev/null 2>&1 || true
DEFAULT_BRANCH="$({GIT} symbolic-ref --quiet --short refs/remotes/origin/HEAD | cut -d/ -f2-)"
if [ -z "$DEFAULT_BRANCH" ]; then
  echo "cannot determine the default branch of origin" >&2
  exit 1
fi
{GIT} fetch origin "+$DEFAULT_BRANCH:refs/remotes/origin/$DEFAULT_BRANCH"
if {GIT} fetch origin "+{branch}:refs/remotes/origin/{branch}"; then
  REMOTE_BRANCH=1
else
  REMOTE_BRANCH=0
fi
if {GIT} show-ref --verify --quiet "refs/heads/{branch}"; then
  {GIT} checkout {branch}
  if [ "$REMOTE_BRANCH" = 1 ]; then
    {GIT} merge --ff-only "refs/remotes/origin/{branch}" || true
  fi
elif [ "$REMOTE_BRANCH" = 1 ]; then
  {GIT} checkout -b {branch} "refs/remotes/origin/{branch}"
else
  {GIT} checkout -b {branch} "refs/remotes/origin/$DEFAULT_BRANCH"
fi
if [ "$REMOTE_BRANCH" = 1 ]; then
  {GIT} branch --set-upstream-to="origin/{branch}" {branch}
else
  {GIT} push -u origin {branch}
fi
{GIT} rev-parse --abbrev-ref --symbolic-full-name "{branch}@{{upstream}}"
"""


def build_token_refresh_script(repo: str, token: str) -> str:
    """The shell that re-points `origin` at a fresh installation token."""
    return (
        f"cd /workspace && "
        f"{GIT} remote set-url origin 'https://x-access-token:{token}@github.com/{repo}'"
    )


async def _exec_script(
    docker: DockerClientWrapper, container_id: str, script: str
) -> tuple[int, str]:
    encoded = base64.b64encode(script.encode()).decode()
    cmd = f"bash -c 'echo {encoded} | base64 -d | bash'"
    return await docker.exec_in_container(container_id, cmd, timeout=30)


async def checkout_branch(
    docker: DockerClientWrapper, container_id: str, branch: str, worker_id: str
) -> bool:
    """Checkout a story branch in the workspace.

    Creates the branch from the repository's up-to-date default branch when it
    does not exist yet, or resumes it at its remote tip when it does, and
    establishes the upstream `git push` needs. Returns False when the branch or
    its upstream was not established.
    """
    logger.info("checkout_branch_start", worker_id=worker_id, branch=branch)
    exit_code, output = await _exec_script(docker, container_id, build_checkout_script(branch))
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
    exit_code, output = await _exec_script(
        docker, container_id, build_token_refresh_script(repo, token)
    )
    if exit_code != 0:
        logger.error("git_token_refresh_failed", worker_id=worker_id, error=output)
        return False
    logger.info("git_token_refreshed", worker_id=worker_id, repo=repo)
    return True
