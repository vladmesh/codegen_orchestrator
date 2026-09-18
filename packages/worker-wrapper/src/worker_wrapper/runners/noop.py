"""The no-LLM developer runner: a *scripted* developer.

The runner builds a self-contained ``python3 -c`` script that runs inside the worker
container, in the workspace, with no model call. It has two modes:

* **Scripted** — ``/workspace/TASK.md`` carries a change set. The script applies the
  change set, runs the product's documented setup (``make setup``), stages and commits
  with the product's own hooks active, and pushes the current branch.
* **Fallback** — no change-set block (or no ``TASK.md``). The script makes an empty
  commit and pushes, which is what the existing noop live suites rely on.

Change-set format
-----------------

The change set travels in the task document the manager already delivers to
``/workspace/TASK.md``; there is no extra queue field, env var or mount. It is a single
fenced block opened by ``codegen-change-set``::

    ```codegen-change-set
    @@ create services/bot/handlers/ping.py
    def ping() -> str:
        return "pong"
    @@ replace README.md
    # Product
    @@ append .env.example
    PING_ENABLED=true
    ```

Rules:

* A directive line is ``@@ <op> <workspace-relative path>``. Everything up to the next
  directive (or the end of the block) is that operation's content; the content is written
  with a trailing newline, and a directive with no content lines means empty content.
* ``create`` writes a new file (parent directories are created) and refuses an existing
  path; ``replace`` overwrites an existing file; ``append`` appends to an existing file.
* Operations are applied in the order written.
* Anything else is a ``MalformedChangeSet``: an unterminated fence, an empty block, an
  unknown op, a directive without a path, or content before the first directive.
* A path that escapes the workspace — absolute, ``~``-rooted, or containing a ``..``
  component — is refused with ``PathEscapesWorkspace``. The whole change set is parsed and
  every path validated *before* the first write, so a rejected change set writes nothing.

Failure reporting
-----------------

Every step is named (``change_set``, ``setup``, ``hooks_path``, ``branch``, ``stage``,
``commit``, ``push``). The first failure stops the run and is POSTed to the result
endpoint as ``success: false`` with the step name, its exit code and its stderr,
credential-redacted and truncated to the last 4000 characters, and the script exits with
that code. A ``make setup`` or hook failure is a failure of the run, never a skip.

Timeouts
--------

``make setup`` gets 1800 s because it builds several uv virtualenvs from scratch, runs the
framework code generation and then ruff over the whole tree; the hook-running ``git
commit`` and ``git push`` get 600 s each because the product's ``.githooks`` run the same
lint and spec checks on every commit. Plain metadata git calls keep 60 s.
"""

from dataclasses import dataclass

from .base import AgentRunner

_SCRIPT = r'''
import json
import os
import re
import subprocess
from urllib.request import Request, urlopen

WORKSPACE = "/workspace"
TASK_FILENAME = "TASK.md"
CHANGE_SET_MARKER = "codegen-change-set"
DIRECTIVE = "@@ "
OPERATIONS = ("create", "replace", "append")
RESULT_URL = "http://127.0.0.1:9090/result"

SETUP_TIMEOUT_SECONDS = 1800
HOOK_TIMEOUT_SECONDS = 600
GIT_TIMEOUT_SECONDS = 60
MAX_STDERR_CHARS = 4000

URL_CREDENTIALS = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s:@]+(?::[^/\s@]*)?@")
TOKEN_LIKE = re.compile(
    r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{10,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{10,}"
    r"|\bsk-[A-Za-z0-9_-]{10,}"
)


class ChangeSetError(Exception):
    """Base class for a change set we refuse to apply."""


class MalformedChangeSet(ChangeSetError):
    pass


class PathEscapesWorkspace(ChangeSetError):
    pass


class ChangeSetTargetExists(ChangeSetError):
    pass


class ChangeSetTargetMissing(ChangeSetError):
    pass


class StepFailed(Exception):
    def __init__(self, step, exit_code, stderr, error_class):
        super().__init__(step)
        self.step = step
        self.exit_code = exit_code or 1
        self.stderr = stderr or ""
        self.error_class = error_class


def redact(text):
    """Strip credentials a git or make diagnostic may echo, then bound the length."""
    cleaned = URL_CREDENTIALS.sub(r"\1***@", text or "")
    cleaned = TOKEN_LIKE.sub("***", cleaned)
    return cleaned[-MAX_STDERR_CHARS:]


def validate_path(path):
    candidate = path.strip()
    if not candidate:
        raise MalformedChangeSet("change-set directive has no path")
    if candidate.startswith("/") or candidate.startswith("~"):
        raise PathEscapesWorkspace("change-set path escapes the workspace: " + candidate)
    parts = [part for part in candidate.split("/") if part]
    if ".." in parts:
        raise PathEscapesWorkspace("change-set path escapes the workspace: " + candidate)
    return candidate


def parse_change_set(text):
    """Return a list of (op, path, content), or None when there is no block."""
    lines = (text or "").splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == "```" + CHANGE_SET_MARKER:
            start = index + 1
            break
    if start is None:
        return None
    end = None
    for index in range(start, len(lines)):
        if lines[index].strip() == "```":
            end = index
            break
    if end is None:
        raise MalformedChangeSet("change-set block is never closed")

    operations = []
    for line in lines[start:end]:
        if line.startswith(DIRECTIVE):
            parts = line[len(DIRECTIVE):].strip().split(None, 1)
            if not parts:
                raise MalformedChangeSet("change-set directive has no operation")
            op = parts[0]
            if op not in OPERATIONS:
                raise MalformedChangeSet("unknown change-set operation: " + op)
            path = validate_path(parts[1] if len(parts) > 1 else "")
            operations.append((op, path, []))
        elif operations:
            operations[-1][2].append(line)
        elif line.strip():
            raise MalformedChangeSet("change-set content before the first directive")
    if not operations:
        raise MalformedChangeSet("change-set block holds no operations")
    return [
        (op, path, "\n".join(content) + "\n" if content else "")
        for op, path, content in operations
    ]


def apply_change_set(operations, workspace):
    for op, path, content in operations:
        target = os.path.join(workspace, path)
        if op == "create":
            if os.path.exists(target):
                raise ChangeSetTargetExists("create target already exists: " + path)
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(content)
        elif op == "replace":
            if not os.path.isfile(target):
                raise ChangeSetTargetMissing("replace target does not exist: " + path)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(content)
        else:
            if not os.path.isfile(target):
                raise ChangeSetTargetMissing("append target does not exist: " + path)
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(content)


def read_task_document(workspace):
    task_path = os.path.join(workspace, TASK_FILENAME)
    try:
        with open(task_path, encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def step(name, args, timeout, workspace, error_class="CommandFailed"):
    try:
        result = subprocess.run(
            args,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise StepFailed(name, 124, "timed out after %s seconds" % timeout, "StepTimedOut")
    if result.returncode != 0:
        raise StepFailed(name, result.returncode, result.stderr or result.stdout, error_class)
    return result


def current_branch(workspace):
    result = step(
        "branch",
        ("git", "rev-parse", "--abbrev-ref", "HEAD"),
        GIT_TIMEOUT_SECONDS,
        workspace,
        "GitCommandFailed",
    )
    branch = result.stdout.strip()
    if not branch or branch == "HEAD":
        raise StepFailed("branch", 1, "no branch is checked out", "DetachedHead")
    return branch


def run_scripted(operations, workspace):
    try:
        apply_change_set(operations, workspace)
    except ChangeSetError as error:
        raise StepFailed("change_set", 1, str(error), type(error).__name__)
    branch = current_branch(workspace)
    step("setup", ("make", "setup"), SETUP_TIMEOUT_SECONDS, workspace, "SetupFailed")
    hooks = step(
        "hooks_path",
        ("git", "config", "--get", "core.hooksPath"),
        GIT_TIMEOUT_SECONDS,
        workspace,
        "HooksPathNotConfigured",
    )
    if hooks.stdout.strip() != ".githooks":
        raise StepFailed(
            "hooks_path",
            1,
            "core.hooksPath is %r after setup, expected '.githooks'" % hooks.stdout.strip(),
            "HooksPathNotConfigured",
        )
    step("stage", ("git", "add", "-A"), GIT_TIMEOUT_SECONDS, workspace, "GitCommandFailed")
    step(
        "commit",
        ("git", "commit", "-m", "feat: apply scripted change set"),
        HOOK_TIMEOUT_SECONDS,
        workspace,
        "CommitFailed",
    )
    step(
        "push",
        ("git", "push", "origin", branch),
        HOOK_TIMEOUT_SECONDS,
        workspace,
        "PushFailed",
    )
    return branch, "scripted change set (%d ops) committed and pushed to %s" % (
        len(operations),
        branch,
    )


def run_fallback(workspace):
    branch = current_branch(workspace)
    step(
        "commit",
        ("git", "commit", "--allow-empty", "-m", "chore: noop marker for e2e test"),
        HOOK_TIMEOUT_SECONDS,
        workspace,
        "CommitFailed",
    )
    step(
        "push",
        ("git", "push", "origin", branch),
        HOOK_TIMEOUT_SECONDS,
        workspace,
        "PushFailed",
    )
    return branch, "noop commit pushed to " + branch


def report(payload):
    request = Request(
        RESULT_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError("result reporting failed: HTTP %s" % response.status)


def main(workspace=WORKSPACE):
    try:
        try:
            operations = parse_change_set(read_task_document(workspace))
        except ChangeSetError as error:
            raise StepFailed("change_set", 1, str(error), type(error).__name__)
        if operations:
            summary = run_scripted(operations, workspace)[1]
        else:
            summary = run_fallback(workspace)[1]
        sha = step(
            "sha", ("git", "rev-parse", "HEAD"), GIT_TIMEOUT_SECONDS, workspace
        ).stdout.strip()
    except StepFailed as error:
        failure = error
    else:
        report({"success": True, "commit": sha, "summary": summary})
        return 0
    stderr = redact(failure.stderr)
    print("noop runner step %s failed (exit %s): %s" % (failure.step, failure.exit_code, stderr))
    report(
        {
            "success": False,
            "reason": "noop runner step %s failed" % failure.step,
            "step": failure.step,
            "error_class": failure.error_class,
            "exit_code": failure.exit_code,
            "stderr": stderr,
        }
    )
    return failure.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
'''


@dataclass
class NoopRunner(AgentRunner):
    """Runner for E2E testing — scripted change set or empty commit, no LLM."""

    def build_command(self, prompt: str) -> list[str]:
        return [
            "python3",
            "-c",
            _SCRIPT,
        ]
