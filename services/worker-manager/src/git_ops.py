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

import asyncio
import base64
from dataclasses import dataclass
import re

import structlog

from shared.git_not_found_retry import git_repository_not_found, retry_delay_after

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
if BRANCH_FETCH_OUTPUT="$({GIT} fetch origin "+{branch}:refs/remotes/origin/{branch}" 2>&1)"; then
  REMOTE_BRANCH=1
else
  printf '%s\\n' "$BRANCH_FETCH_OUTPUT" >&2
  case "$BRANCH_FETCH_OUTPUT" in
    *"couldn't find remote ref"*) REMOTE_BRANCH=0 ;;
    *) exit 1 ;;
  esac
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


_OUTPUT_TAIL_CHARS = 2000
_CONTAINER_LOG_TAIL_LINES = 20
_CREDENTIAL_IN_URL = re.compile(r"(https?://)[^/@\s]+@")


def _tail(raw: bytes | str | None) -> str:
    """The redacted last characters of one stream, for a log line and an error."""
    if raw is None:
        return ""
    text = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
    text = _CREDENTIAL_IN_URL.sub(r"\1***@", text).strip()
    return text[-_OUTPUT_TAIL_CHARS:]


@dataclass(frozen=True)
class CheckoutResult:
    """What one checkout did. Truthy exactly when the branch and upstream exist.

    `detail` is the account of a failure, never empty: the exit code, what the
    script wrote to each stream, and — when the script wrote nothing — what
    Docker says became of the container it ran in.
    """

    ok: bool
    detail: str = ""

    def __bool__(self) -> bool:
        return self.ok


async def _container_account(docker: DockerClientWrapper, container_id: str) -> str:
    """Whether the worker container is still running, and its last log lines if not.

    A checkout that exits without a word is almost never git's doing: git always
    says why it failed. It is the container the exec ran in going away under it
    (Docker reports the exec as killed, 137, with empty output). The container's
    own log is then the only place the reason is written.
    """
    try:
        attrs = await docker.inspect_container(container_id)
    except Exception as exc:  # noqa: BLE001 — the account is evidence, not a second failure
        return f"the worker container could not be inspected: {type(exc).__name__}: {exc}"
    state = (attrs or {}).get("State") or {}
    if state.get("Running"):
        return "the worker container is still running"
    account = (
        f"the worker container is not running (status={state.get('Status', 'unknown')}, "
        f"exit_code={state.get('ExitCode', 'unknown')})"
    )
    try:
        logs = await docker.read_container_logs(container_id, tail=_CONTAINER_LOG_TAIL_LINES)
    except Exception as exc:  # noqa: BLE001 — a missing log still leaves the state above
        return f"{account}; its log could not be read: {type(exc).__name__}: {exc}"
    logs_tail = _tail(logs)
    return f"{account}; its log ends: {logs_tail}" if logs_tail else account


async def checkout_branch(
    docker: DockerClientWrapper, container_id: str, branch: str, worker_id: str
) -> CheckoutResult:
    """Checkout a story branch in the workspace.

    Creates the branch from the repository's up-to-date default branch when it
    does not exist yet, or resumes it at its remote tip when it does, and
    establishes the upstream `git push` needs. The result is falsy, with a
    non-empty `detail`, when the branch or its upstream was not established.
    """
    logger.info("checkout_branch_start", worker_id=worker_id, branch=branch)
    encoded = base64.b64encode(build_checkout_script(branch).encode()).decode()
    cmd = f"bash -c 'echo {encoded} | base64 -d | bash'"
    attempt = 0
    while True:
        attempt += 1
        exit_code, stdout, stderr = await docker.exec_capture(container_id, cmd, timeout=30)
        if exit_code == 0:
            logger.info(
                "checkout_branch_complete", worker_id=worker_id, branch=branch, attempts=attempt
            )
            return CheckoutResult(ok=True)
        delay = retry_delay_after(attempt) if git_repository_not_found(stderr, stdout) else None
        if delay is None:
            break
        logger.warning(
            "checkout_branch_retry",
            worker_id=worker_id,
            branch=branch,
            attempt=attempt,
            delay_seconds=delay,
            stderr=_tail(stderr),
            stdout=_tail(stdout),
        )
        await asyncio.sleep(delay)

    stdout_tail, stderr_tail = _tail(stdout), _tail(stderr)
    parts = [f"exit_code={exit_code}"]
    if stderr_tail:
        parts.append(f"stderr: {stderr_tail}")
    if stdout_tail:
        parts.append(f"stdout: {stdout_tail}")
    if attempt > 1:
        parts.append(f"attempts={attempt}")
    container = None
    if not stderr_tail and not stdout_tail:
        container = await _container_account(docker, container_id)
        parts.append(f"no output; {container}")
    detail = "; ".join(parts)
    logger.error(
        "checkout_branch_failed",
        worker_id=worker_id,
        branch=branch,
        exit_code=exit_code,
        stderr=stderr_tail,
        stdout=stdout_tail,
        container=container,
        attempts=attempt,
    )
    return CheckoutResult(ok=False, detail=detail)


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
