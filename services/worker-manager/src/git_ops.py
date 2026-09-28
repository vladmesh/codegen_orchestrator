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
import shlex

import structlog

from shared.diagnostics import redact_diagnostic
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


# Container-private storage, never a bind mount or a workspace auth artifact.
GIT_CREDENTIAL_PATH = "/home/worker/.config/codegen/git-credentials"


def build_token_refresh_script(repo: str) -> str:
    """Upgrade the released origin and atomically refresh a private credential.

    Docker passes the token in its native exec environment. No token or encoding
    of it is part of this script, Git argv or persisted workspace configuration.
    """
    writer = r"""import os
from pathlib import Path
import shlex
import subprocess
import tempfile
from urllib.parse import quote
path = Path(os.environ["CODEGEN_GIT_CREDENTIAL_PATH"])
path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
path.parent.chmod(0o700)
fd, temporary = tempfile.mkstemp(dir=path.parent)
try:
    with os.fdopen(fd, "w") as stream:
        token = quote(os.environ["GITHUB_TOKEN"], safe="")
        repo = os.environ["CODEGEN_GIT_REPO"]
        stream.write(f"https://x-access-token:{token}@github.com/{repo}.git\n")
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)

# HOME survives the wrapper's agent environment filter. Configure native Git
# once for both manager and agent processes, preserving unrelated global keys.
config = Path(os.environ["HOME"]) / ".gitconfig"
fd, temporary = tempfile.mkstemp(dir=config.parent)
try:
    with os.fdopen(fd, "wb") as stream:
        if config.exists():
            stream.write(config.read_bytes())
    command = ["git", "-c", "core.hooksPath=/dev/null", "config", "--file", temporary]
    scope = "credential.https://github.com"
    subprocess.run([*command, "--replace-all", scope + ".helper", ""], check=True)
    subprocess.run(
        [*command, "--add", scope + ".helper", "store --file=" + shlex.quote(str(path))],
        check=True,
    )
    subprocess.run([*command, "--replace-all", scope + ".useHttpPath", "true"], check=True)
    os.replace(temporary, config)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
"""
    clean_url = shlex.quote(f"https://github.com/{repo}.git")
    return (
        f"set -e\ncd /workspace\n{GIT} remote set-url origin {clean_url}\n"
        f"python3 -I -c {shlex.quote(writer)}"
    )


_OUTPUT_TAIL_CHARS = 2000
_CONTAINER_LOG_TAIL_LINES = 20


def _tail(raw: bytes | str | None, secrets: tuple[str, ...] = ()) -> str:
    """The redacted last characters of one stream, for a log line and an error."""
    if raw is None:
        return ""
    text = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
    text = redact_diagnostic(text, secrets=secrets).strip()
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


async def _container_account(
    docker: DockerClientWrapper, container_id: str, secrets: tuple[str, ...] = ()
) -> str:
    """Whether the worker container is still running, and its last log lines if not.

    A checkout that exits without a word is almost never git's doing: git always
    says why it failed. It is the container the exec ran in going away under it
    (Docker reports the exec as killed, 137, with empty output). The container's
    own log is then the only place the reason is written.
    """
    try:
        attrs = await docker.inspect_container(container_id)
    except Exception as exc:  # noqa: BLE001 — the account is evidence, not a second failure
        return (
            f"the worker container could not be inspected: {type(exc).__name__}: "
            f"{_tail(str(exc), secrets)}"
        )
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
        return (
            f"{account}; its log could not be read: {type(exc).__name__}: "
            f"{_tail(str(exc), secrets)}"
        )
    logs_tail = _tail(logs, secrets)
    return f"{account}; its log ends: {logs_tail}" if logs_tail else account


async def checkout_branch(
    docker: DockerClientWrapper,
    container_id: str,
    branch: str,
    worker_id: str,
    *,
    secret_values: tuple[str, ...] = (),
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
        try:
            exit_code, stdout, stderr = await docker.exec_capture(container_id, cmd, timeout=30)
        except Exception as exc:  # noqa: BLE001 - return a safe checkout failure
            detail = f"{type(exc).__name__}: {_tail(str(exc), secret_values)}"
            logger.error("checkout_branch_failed", worker_id=worker_id, error=detail)
            return CheckoutResult(ok=False, detail=detail)
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
            stderr=_tail(stderr, secret_values),
            stdout=_tail(stdout, secret_values),
        )
        await asyncio.sleep(delay)

    stdout_tail, stderr_tail = _tail(stdout, secret_values), _tail(stderr, secret_values)
    parts = [f"exit_code={exit_code}"]
    if stderr_tail:
        parts.append(f"stderr: {stderr_tail}")
    if stdout_tail:
        parts.append(f"stdout: {stdout_tail}")
    if attempt > 1:
        parts.append(f"attempts={attempt}")
    container = None
    if not stderr_tail and not stdout_tail:
        container = await _container_account(docker, container_id, secret_values)
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
    """Sanitize origin and supply credentials for the container's Git lifetime."""
    environment = {
        "GITHUB_TOKEN": token,
        "CODEGEN_GIT_REPO": repo,
        "CODEGEN_GIT_CREDENTIAL_PATH": GIT_CREDENTIAL_PATH,
    }
    try:
        exit_code, output = await docker.exec_in_container(
            container_id,
            ["bash", "-c", build_token_refresh_script(repo)],
            timeout=30,
            environment=environment,
        )
    except Exception as exc:  # noqa: BLE001 - fail preparation without leaking SDK output
        logger.error(
            "git_token_refresh_failed",
            worker_id=worker_id,
            error=_tail(str(exc), (token,)),
            error_type=type(exc).__name__,
        )
        return False
    if exit_code != 0:
        logger.error("git_token_refresh_failed", worker_id=worker_id, error=_tail(output, (token,)))
        return False
    logger.info("git_token_refreshed", worker_id=worker_id, repo=repo)
    return True
