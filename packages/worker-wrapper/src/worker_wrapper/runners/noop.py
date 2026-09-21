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

The change set travels in the task document the manager already writes to
``/workspace/TASK.md``; there is no extra queue field, env var, mount or manager
transport. It is a single fenced block opened by ``codegen-change-set`` whose first line
is the version sentinel ``codegen-change-set v1``::

    ```codegen-change-set
    codegen-change-set v1
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
* Anything else is a ``MalformedChangeSet``: an unterminated fence, an empty block, a
  missing or unsupported version sentinel, an unknown op, a directive without a path, or
  content before the first directive.
* A path that escapes the workspace — absolute, ``~``-rooted, or containing a ``..``
  component — is refused with ``PathEscapesWorkspace``. The whole block is parsed and
  every path validated *before* the first write, so a refused change set writes nothing.

Why the marker alone is not enough: since card 1301 the task document quotes a card's
acceptance criteria verbatim, so a card that *documents* this format would otherwise have
its documentation executed. Hence the sentinel first line, and hence a document holding
more than one block opened by the marker is refused as ``MalformedChangeSet`` instead of
silently running the first one.

Two limitations of the format, deliberate and bounded:

* A *content* line that itself begins with ``@@ `` is read as a directive, so a change set
  cannot carry a file whose content contains a diff hunk header. The change sets are
  written by this repository's own harness, not by users, so the escape a general format
  would need buys nothing here.
* ``validate_path`` is a pure string check and does not resolve symlinks: a symlink already
  present in the workspace could still redirect a write outside it. The threat model is a
  harness-authored change set applied to a freshly scaffolded product workspace, so the
  guard exists to catch a malformed path, not an attacker who can already plant a symlink
  inside the workspace — anybody who can do that can write the file directly.

Hooks
-----

The two modes treat the product's hooks in opposite ways, on purpose:

* Scripted — the hooks must run, because that is what the card is for. ``make setup``
  ends with ``git config core.hooksPath .githooks``, the script verifies that with
  ``git config --local --get core.hooksPath`` (``--local``, so a global or system value
  cannot satisfy it), and the commit then runs the product's own gate.
* Fallback — the hooks must not run. Its workspace is a scaffolded product whose config
  already has ``core.hooksPath=.githooks`` (``services/scaffolder/src/scaffold.py``), so
  an "empty" commit would otherwise fire ``pre-commit``, whose ``git add -A`` stages the
  worker's injected files, and then a full-lint ``pre-push``. The fallback therefore runs
  its commit and push as ``git -c core.hooksPath=/dev/null …`` — per process, the form
  ``services/worker-manager/src/git_ops.py`` already uses — and never writes
  ``core.hooksPath`` into the workspace config.

Before the scripted commit the script adds the orchestrator's own injected paths to
``.git/info/exclude``, because the product's ``pre-commit`` hook runs ``git add -A`` and
would otherwise publish orchestrator-internal files into the product repository.
``info/exclude`` is workspace-local, so this changes nothing in the product kit. The set
is not written here: ``worker_wrapper.injected_paths`` defines it once for the wrapper's
exclude writer, its publish guard and this script, and the script — which runs in a
separate process, built from a string — receives the same lines as data.

Failure reporting
-----------------

Every step is named (``change_set``, ``branch``, ``setup``, ``hooks_path``, ``exclude``,
``stage``, ``commit``, ``push``, ``sha``). The first failure stops the run and is POSTed
to the result endpoint as ``success: false`` with the step name, its exit code and its
stderr, credential-redacted and truncated to the last 4000 characters, and the script
exits with that code. A ``make setup`` or hook failure is a failure of the run, never a
skip.

Timeouts
--------

``make setup`` gets 1800 s because it builds several uv virtualenvs from scratch, runs the
framework code generation and then ruff over the whole tree; the hook-running ``git
commit`` and ``git push`` get 600 s each because the product's ``.githooks`` run the same
lint and spec checks on every commit. Plain metadata git calls keep 60 s.
"""

from dataclasses import dataclass

from ..injected_paths import EXCLUDE_HEADER, EXCLUDE_LINES
from .base import AgentRunner

_SCRIPT_TEMPLATE = r'''
import json
import os
import re
import subprocess
from urllib.request import Request, urlopen

WORKSPACE = "/workspace"
TASK_FILENAME = "TASK.md"
CHANGE_SET_MARKER = "codegen-change-set"
CHANGE_SET_SENTINEL = "codegen-change-set v1"
EXCLUDE_HEADER = __EXCLUDE_HEADER__
DIRECTIVE = "@@ "
OPERATIONS = ("create", "replace", "append")
RESULT_URL = "http://127.0.0.1:9090/result"

# Injected by the orchestrator, never part of the product repository. Compiled in
# from worker_wrapper.injected_paths, which is the one definition of the set.
WORKER_INTERNAL_FILES = __WORKER_INTERNAL_FILES__

# Per process, never written into the workspace config: the scripted path needs the
# product's hooks, so the fallback may not disable them for anybody but itself.
HOOKLESS_GIT = ("git", "-c", "core.hooksPath=/dev/null")

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


def candidate_blocks(lines):
    """Every fenced block whose opening line is exactly the change-set marker."""
    blocks = []
    index = 0
    while index < len(lines):
        if lines[index].strip() != "```" + CHANGE_SET_MARKER:
            index += 1
            continue
        end = None
        for scan in range(index + 1, len(lines)):
            if lines[scan].strip() == "```":
                end = scan
                break
        if end is None:
            raise MalformedChangeSet("change-set block is never closed")
        blocks.append(lines[index + 1:end])
        index = end + 1
    return blocks


def parse_directives(body):
    operations = []
    for line in body:
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


def parse_change_set(text):
    """Return a list of (op, path, content), or None when there is no block."""
    blocks = candidate_blocks((text or "").splitlines())
    if not blocks:
        return None
    if len(blocks) > 1:
        raise MalformedChangeSet(
            "task document holds %d change-set blocks; refusing to guess" % len(blocks)
        )
    body = blocks[0]
    if not body or body[0].strip() != CHANGE_SET_SENTINEL:
        raise MalformedChangeSet(
            "change-set block does not open with the sentinel %r" % CHANGE_SET_SENTINEL
        )
    return parse_directives(body[1:])


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


def exclude_worker_internal_files(workspace):
    """Keep the manager's injected files out of the product's commit.

    The product's `pre-commit` hook runs `git add -A`, so an ignore rule is the only
    thing between an orchestrator-internal instruction file and the product repository.
    `.git/info/exclude` is workspace-local, so the product kit stays untouched.
    """
    located = step(
        "exclude",
        ("git", "rev-parse", "--git-path", "info/exclude"),
        GIT_TIMEOUT_SECONDS,
        workspace,
        "GitCommandFailed",
    )
    relative = located.stdout.strip()
    path = relative if os.path.isabs(relative) else os.path.join(workspace, relative)
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        existing = ""
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                existing = handle.read()
        present = [line.strip() for line in existing.splitlines()]
        missing = [name for name in WORKER_INTERNAL_FILES if name not in present]
        if not missing:
            return
        with open(path, "a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(EXCLUDE_HEADER + "\n")
            handle.write("\n".join(missing) + "\n")
    except OSError as error:
        raise StepFailed("exclude", 1, str(error), "ExcludeWriteFailed")


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
        ("git", "config", "--local", "--get", "core.hooksPath"),
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
    exclude_worker_internal_files(workspace)
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
        HOOKLESS_GIT + ("commit", "--allow-empty", "-m", "chore: noop marker for e2e test"),
        HOOK_TIMEOUT_SECONDS,
        workspace,
        "CommitFailed",
    )
    step(
        "push",
        HOOKLESS_GIT + ("push", "origin", branch),
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


_SCRIPT = _SCRIPT_TEMPLATE.replace("__WORKER_INTERNAL_FILES__", repr(list(EXCLUDE_LINES))).replace(
    "__EXCLUDE_HEADER__", repr(EXCLUDE_HEADER)
)


@dataclass
class NoopRunner(AgentRunner):
    """Runner for E2E testing — scripted change set or empty commit, no LLM."""

    def build_command(self, prompt: str) -> list[str]:
        return [
            "python3",
            "-c",
            _SCRIPT,
        ]
