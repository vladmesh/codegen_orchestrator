"""Git operations for worker containers (checkout and on-demand credentials).

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
    - Synchronize local and remote tips only by fast-forward. A local tip ahead
      of remote is kept; divergent tips fail without discarding either side.
    - Advance to default only when the synchronized tip is already contained
      there. Publish that advance by a native non-force push.
    - The push that establishes the upstream is not swallowed, and the script
      ends by reading the upstream back, so a checkout that did not do its job
      exits non-zero.
    """
    return f"""set -e
cd /workspace
TRACKED_STATUS="$({GIT} status --porcelain --untracked-files=no)"
if [ -n "$TRACKED_STATUS" ]; then
  echo "workspace has unfinished tracked changes; checkout refused" >&2
  exit 1
fi
is_ancestor() {{
  if {GIT} merge-base --is-ancestor "$1" "$2"; then
    return 0
  else
    ANCESTRY_STATUS=$?
    if [ "$ANCESTRY_STATUS" = 1 ]; then return 1; fi
    exit "$ANCESTRY_STATUS"
  fi
}}
ORIGIN_HEAD="$({GIT} ls-remote --symref origin HEAD)"
DEFAULT_BRANCH="$(printf '%s\\n' "$ORIGIN_HEAD" | awk '
  $1 == "ref:" && $3 == "HEAD" {{sub("^refs/heads/", "", $2); print $2}}')"
if [ -z "$DEFAULT_BRANCH" ]; then
  echo "cannot determine the default branch of origin" >&2
  exit 1
fi
{GIT} fetch origin "+$DEFAULT_BRANCH:refs/remotes/origin/$DEFAULT_BRANCH"
{GIT} remote set-head origin "$DEFAULT_BRANCH"
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
    if is_ancestor "refs/remotes/origin/{branch}" HEAD; then
      :
    else
      {GIT} merge --ff-only "refs/remotes/origin/{branch}"
    fi
  fi
elif [ "$REMOTE_BRANCH" = 1 ]; then
  {GIT} checkout -b {branch} "refs/remotes/origin/{branch}"
else
  {GIT} checkout -b {branch} "refs/remotes/origin/$DEFAULT_BRANCH"
fi
if is_ancestor HEAD "refs/remotes/origin/$DEFAULT_BRANCH"; then
  {GIT} merge --ff-only "refs/remotes/origin/$DEFAULT_BRANCH"
fi
# Publish the preserved local tip too: the remote and local pre-turn evidence
# must agree, including work left unpushed by an earlier interrupted worker.
{GIT} push -u origin {branch}
{GIT} rev-parse --abbrev-ref --symbolic-full-name "{branch}@{{upstream}}"
LOCAL_HEAD="$({GIT} rev-parse HEAD)"
REMOTE_READBACK="$({GIT} ls-remote --exit-code origin "refs/heads/{branch}")"
REMOTE_HEAD="${{REMOTE_READBACK%%[[:space:]]*}}"
if [ "$LOCAL_HEAD" != "$REMOTE_HEAD" ]; then
  echo "published story branch head does not match local HEAD" >&2
  exit 1
fi
printf 'CODEGEN_CHECKOUT_HEAD=%s\\n' "$LOCAL_HEAD"
"""


def build_credential_setup_script(repo: str) -> str:
    """Remove released auth artifacts and install the native on-demand helper."""
    writer = r"""import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit, urlunsplit
home = Path(os.environ["HOME"])
for path in (
    home / ".config/codegen/git-credentials", home / ".git-credentials",
    home / ".config/gh/hosts.yml",
):
    path.unlink(missing_ok=True)
# Remove local overrides which could bypass the platform helper and all HTTP
# auth headers. Preserve product hooks, refs, worktree and unrelated settings.
for scope in ("--local", "--global"):
    command = ["git", "-c", "core.hooksPath=/dev/null", "config", scope]
    keys = subprocess.run(
        [*command, "--name-only", "--get-regexp", r"^(credential\.|http\..*extraheader$)"],
        capture_output=True, text=True,
    )
    if keys.returncode not in (0, 1):
        raise RuntimeError("cannot read Git configuration")
    for key in set(keys.stdout.splitlines()):
        if (
            scope == "--local" or key.startswith("credential.https://github.com")
            or key.startswith("credential.helper") or key.startswith("http.")
        ):
            subprocess.run([*command, "--unset-all", key], check=True)
    urls = subprocess.run(
        [*command, "--get-regexp", r"^remote\..*\.(url|pushurl)$"],
        capture_output=True, text=True,
    )
    if urls.returncode not in (0, 1):
        raise RuntimeError("cannot read remote configuration")
    for line in urls.stdout.splitlines():
        key, value = line.split(" ", 1)
        url = urlsplit(value)
        if url.hostname == "github.com" and (url.username or url.password):
            clean = urlunsplit(("https", "github.com", url.path, "", ""))
            subprocess.run([*command, "--replace-all", key, clean], check=True)
command = ["git", "-c", "core.hooksPath=/dev/null", "config", "--global"]
subprocess.run([*command, "--replace-all", "credential.https://github.com.helper", ""], check=True)
subprocess.run([
    *command, "--add", "credential.https://github.com.helper",
    "/usr/local/bin/git-credential-codegen",
], check=True)
subprocess.run([
    *command, "--replace-all", "credential.https://github.com.useHttpPath", "true",
], check=True)
"""
    clean_url = shlex.quote(f"https://github.com/{repo}.git")
    return (
        f"set -e\ncd /workspace\npython3 -I -c {shlex.quote(writer)}\n"
        f"{GIT} remote set-url origin {clean_url}\n"
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
    head_sha: str | None = None

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
            output = stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout
            heads = [
                line.removeprefix("CODEGEN_CHECKOUT_HEAD=")
                for line in (output or "").splitlines()
                if line.startswith("CODEGEN_CHECKOUT_HEAD=")
            ]
            if len(heads) != 1 or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", heads[0]) is None:
                return CheckoutResult(ok=False, detail="checkout returned no valid prepared head")
            logger.info(
                "checkout_branch_complete", worker_id=worker_id, branch=branch, attempts=attempt
            )
            return CheckoutResult(ok=True, head_sha=heads[0])
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


async def configure_git_credentials(
    docker: DockerClientWrapper, container_id: str, repo: str, worker_id: str
) -> bool:
    """Sanitize the checkout without injecting or storing a GitHub token."""
    try:
        exit_code, _ = await docker.exec_in_container(
            container_id,
            ["bash", "-c", build_credential_setup_script(repo)],
            timeout=30,
        )
    except Exception:  # noqa: BLE001 - released config can contain unknown secrets
        logger.error("git_credential_setup_failed", worker_id=worker_id)
        return False
    if exit_code != 0:
        logger.error("git_credential_setup_failed", worker_id=worker_id)
        return False
    return True
