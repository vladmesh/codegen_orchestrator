"""What only the *second* story of a project can show, stated as predicates.

Five of the seven regressions sprint:1445 found by hand lived only in a
project's second story, and every one of them is a property of a *reused*
workspace or of a project that already has an owner. None of them is visible in
the first story, and none of them is an assertion about success: a second story
can deploy, pass QA and complete while still having taken the wrong path to get
there.

So each property below is a function from an observation to the list of reasons
that observation does not hold it, the way
`level1_brief.bot_completion_message_mismatches` already states a message
contract. An empty list means the property holds. That shape is what lets the
live suite and the offline regressions judge one thing: the offline tests feed
these the fixtures a defect would produce and assert they say so, and a
predicate that answered `[]` for a broken fixture is caught there rather than on
the stand.

The properties, and the production evidence each one is here for:

* **`checkout_mismatches`** — `issue:028670f21dbd138ccd04`. On production, twice,
  the first `checkout_branch` of a second story hit the manager's 30 s exec
  timeout, the manager deleted the worker, the run failed "Worker container
  died", and the automatic retry succeeded in about four seconds. So the run
  reads the manager's own `checkout_branch_start`/`checkout_branch_complete`
  pair for this story's branch and states how long it took as a number.
* **`product_hook_mismatches`** — card 1305. The reused workspace carries the
  product's `core.hooksPath=.githooks` from the first story's `make setup`, and
  the manager's infrastructure git ran the product's `pre-push` hook with it.
* **`branch_base_mismatches`** — card 1305 again, its other half: the story
  branch was cut from the reused workspace's stale HEAD instead of the remote
  default branch. Containment is proved from a comparison, never assumed.
* **`deploy_path_mismatches`** — a project that already has a completed story
  deploys through the PR poller; only its first story goes through the initial
  owner grant. Success says nothing about which of the two happened, so the path
  is read off the Run that carried it.
* **`engineering_run_mismatches`** — a second story that needed a retry to get
  going is not a second story that worked. The failed attempt is durable, so it
  is read rather than inferred from the task's final status.
* **`ci_run_mismatches`** — the project's own `ci.yml` is what publishes the
  images the deploy pulls, and it runs for the *merge* commit, not the pull
  request head.
"""

from __future__ import annotations

from datetime import datetime
import json
import re

# ── The deploy path a Run took ───────────────────────────────────────────

#: The scheduler's merged-PR poller: `deploy-poll-<hex>` with
#: `run_metadata.triggered_by == "pr_poll"`
#: (`services/scheduler/src/tasks/pr_poller.py`).
DEPLOY_PATH_PR_POLLER = "pr_poller"
#: The API-owned initial-owner grant lifecycle: `deploy-grant-<hex>` with
#: `run_metadata.triggered_by == "users_grant_intent"`
#: (`services/api/src/routers/projects/access.py`). Only a project's *first*
#: deploy of a `tg_bot` product takes it.
DEPLOY_PATH_OWNER_GRANT = "owner_grant"

PR_POLLER_RUN_PREFIX = "deploy-poll-"
OWNER_GRANT_RUN_PREFIX = "deploy-grant-"
PR_POLLER_TRIGGER = "pr_poll"
OWNER_GRANT_TRIGGER = "users_grant_intent"

#: Both halves of a path's identity, so a Run that carries one and not the other
#: is not classified at all rather than classified by the half that agrees.
_DEPLOY_PATHS = {
    DEPLOY_PATH_PR_POLLER: (PR_POLLER_RUN_PREFIX, PR_POLLER_TRIGGER),
    DEPLOY_PATH_OWNER_GRANT: (OWNER_GRANT_RUN_PREFIX, OWNER_GRANT_TRIGGER),
}


def deploy_path_record(run: dict) -> dict:
    """Which path created this deploy Run, read off the Run itself.

    Two independent facts have to agree — the id the creating path mints and the
    `triggered_by` it stamps — because either alone is one edit away from being
    a coincidence. A Run whose halves disagree, or whose halves name no known
    path, gets `path=None` and keeps both facts, so the caller's reason can
    print what it actually saw.
    """
    run_id = run.get("id") or ""
    triggered_by = (run.get("run_metadata") or {}).get("triggered_by")
    path = next(
        (
            name
            for name, (prefix, trigger) in _DEPLOY_PATHS.items()
            if run_id.startswith(prefix) and triggered_by == trigger
        ),
        None,
    )
    return {
        "run_id": run_id,
        "triggered_by": triggered_by,
        "deploy_action": (run.get("run_metadata") or {}).get("deploy_action"),
        "path": path,
    }


def deploy_path_mismatches(record: dict, *, expected: str) -> list[str]:
    """Why this deploy Run did not take the path it had to take, if it did not."""
    if expected not in _DEPLOY_PATHS:
        raise ValueError(f"unknown deploy path {expected!r}")
    if record.get("path") == expected:
        return []
    prefix, trigger = _DEPLOY_PATHS[expected]
    return [
        f"deploy run {record.get('run_id')!r} triggered_by={record.get('triggered_by')!r} "
        f"is {record.get('path') or 'no recognised path'}, not {expected} "
        f"(which mints {prefix}… and stamps triggered_by={trigger!r})"
    ]


# ── The manager's first checkout of the story branch ─────────────────────

CHECKOUT_START_EVENT = "checkout_branch_start"
CHECKOUT_COMPLETE_EVENT = "checkout_branch_complete"
CHECKOUT_FAILED_EVENT = "checkout_branch_failed"


def _log_records(log_text: str) -> list[dict]:
    """Every JSON record in a `docker compose logs` capture, in order.

    Compose prefixes each line with the service name, so the record starts at
    the first `{"` — the same reading `record_settings_seed_brief_log` makes of
    the deploy consumer's log.
    """
    records = []
    for line in log_text.splitlines():
        start = line.find('{"')
        if start < 0:
            continue
        try:
            record = json.loads(line[start:])
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _log_moment(record: dict) -> datetime | None:
    """The moment a structlog record was written, or None when it is unusable."""
    stamp = record.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def checkout_records(log_text: str, *, branch: str) -> list[dict]:
    """Every `checkout_branch` the manager ran for one branch, oldest first.

    One record per attempt: when it started, when it ended, how long it took and
    whether it ended by failing. A start with no ending is kept as itself — that
    is exactly the shape `issue:028670f21dbd138ccd04` produced, the exec timeout
    that killed the worker before any completion line could be written — and a
    caller that needs an ending says so rather than reading `duration_seconds`
    of None as a pass.
    """
    attempts: list[dict] = []
    open_by_worker: dict[str, dict] = {}
    for record in _log_records(log_text):
        event = record.get("event")
        if event not in {CHECKOUT_START_EVENT, CHECKOUT_COMPLETE_EVENT, CHECKOUT_FAILED_EVENT}:
            continue
        if record.get("branch") != branch:
            continue
        worker_id = str(record.get("worker_id"))
        moment = _log_moment(record)
        if event == CHECKOUT_START_EVENT:
            attempt = {
                "branch": branch,
                "worker_id": worker_id,
                "started_at": record.get("timestamp"),
                "completed_at": None,
                "duration_seconds": None,
                "failed": False,
            }
            attempts.append(attempt)
            open_by_worker[worker_id] = attempt
            continue
        attempt = open_by_worker.pop(worker_id, None)
        if attempt is None:
            # An ending whose start is off the tail this log capture covers.
            # Recorded as itself rather than dropped: it is evidence that the
            # capture is short, not that the checkout did not start.
            attempt = {
                "branch": branch,
                "worker_id": worker_id,
                "started_at": None,
                "completed_at": None,
                "duration_seconds": None,
                "failed": False,
            }
            attempts.append(attempt)
        attempt["completed_at"] = record.get("timestamp")
        attempt["failed"] = event == CHECKOUT_FAILED_EVENT
        started = _log_moment({"timestamp": attempt["started_at"]})
        if started is not None and moment is not None:
            attempt["duration_seconds"] = round((moment - started).total_seconds(), 3)
    return attempts


def checkout_mismatches(attempts: list[dict], *, branch: str, bound_seconds: float) -> list[str]:
    """Why the first checkout of this branch is not the one a green run has.

    Only the *first* attempt is judged. A second one exists only because the
    first failed, and the automatic retry succeeding in four seconds is precisely
    what hid `issue:028670f21dbd138ccd04` on production for two runs.
    """
    if not attempts:
        return [f"the manager ran no checkout_branch for {branch} in the log this run read"]
    first = attempts[0]
    reasons = []
    if first["failed"]:
        reasons.append(f"the first checkout of {branch} failed on worker {first['worker_id']}")
    if first["completed_at"] is None:
        reasons.append(
            f"the first checkout of {branch} never reported an ending, which is what the "
            "30 s exec timeout of issue:028670f21dbd138ccd04 looks like from here"
        )
    elif first["duration_seconds"] is None:
        reasons.append(
            f"the first checkout of {branch} has no readable duration: "
            f"started_at={first['started_at']!r} completed_at={first['completed_at']!r}"
        )
    elif first["duration_seconds"] > bound_seconds:
        reasons.append(
            f"the first checkout of {branch} took {first['duration_seconds']}s, over the "
            f"{bound_seconds}s bound"
        )
    if len(attempts) > 1:
        reasons.append(
            f"{len(attempts)} checkouts of {branch} were run; a retried checkout means the "
            "first one did not establish the branch"
        )
    return reasons


# ── The workspace the story ran in ───────────────────────────────────────

#: What the manager logs when it hands a developer worker the project's one
#: persistent checkout (`services/worker-manager/src/manager.py`). A QA executor
#: gets `using_ephemeral_qa_workspace` instead and is not one of these.
WORKSPACE_ASSIGNED_EVENT = "using_scaffolded_workspace"


def workspace_assignments(log_text: str, *, repo_id: str) -> list[dict]:
    """Every developer workspace the manager assigned for one repository."""
    return [
        {
            "worker_id": record.get("worker_id"),
            "repo_id": record.get("repo_id"),
            "path": record.get("path"),
        }
        for record in _log_records(log_text)
        if record.get("event") == WORKSPACE_ASSIGNED_EVENT and record.get("repo_id") == repo_id
    ]


def workspace_reuse_mismatches(assignments: list[dict], *, repo_id: str) -> list[str]:
    """Why this story did not run in the project's one reused workspace.

    What this proves, exactly. The manager has two ways to give a worker a
    workspace: the project's persistent checkout, which
    `_find_developer_workspace` refuses to invent — it raises when the
    scaffolder's directory is not already there — and an ephemeral directory,
    which only a QA executor gets and which logs a different event. So an
    assignment of `…/<repo_id>` under this event is the scaffolded checkout the
    project already had, and every assignment naming the same path is every
    developer worker of this repository sharing one directory.

    What it does not prove on its own is that the directory's *contents*
    survived, and nothing here claims that. The first checkout's duration is
    what says so: a workspace that had to be re-cloned could not be put on a
    branch in seconds.
    """
    if not assignments:
        return [
            f"the manager assigned no developer workspace for repository {repo_id} in the log "
            "this run read"
        ]
    reasons = []
    paths = sorted({assignment["path"] for assignment in assignments})
    if len(paths) > 1:
        reasons.append(
            f"the workers of repository {repo_id} were given {len(paths)} different "
            f"workspaces: {paths}"
        )
    reasons.extend(
        f"workspace {path!r} is not the scaffolded checkout of repository {repo_id}"
        for path in paths
        if not str(path).rstrip("/").endswith(f"/{repo_id}")
    )
    return reasons


# ── The manager's own git never runs the product's hooks ─────────────────

#: A git command word: `git` at the start of a word, not `.git` or `origin/git`.
#: The same reading `services/worker-manager/tests/unit/test_infra_git_no_product_hooks.py`
#: makes of the same script; stated again here because this judges the script
#: the *running* manager builds, read out of its container, rather than the one
#: this checkout's source would build.
_GIT_COMMAND = re.compile(r"(?<![\w/.-])git\s+")
HOOKLESS_GIT_PREFIX = "-c core.hooksPath=/dev/null "


def product_hook_mismatches(script: str) -> list[str]:
    """Why this script would run the product's hooks, if it would.

    What this proves and what it does not. The workspace is the product's own
    checkout and carries `core.hooksPath=.githooks` from the first story's
    `make setup`; a git invocation that does not neutralise it *will* run the
    product's `pre-push`. So a script whose every invocation carries the
    per-command override cannot run one, and that is a property of the command
    text — decidable here, without asking the workspace anything. It is read out
    of the running manager, so it is that manager's behaviour and not this
    checkout's source that is judged. What it cannot prove on its own is that
    the hooks are still installed for the developer agent; that is the
    `config core.hooksPath` half below, and the behavioural proof against real
    git lives in the manager's own unit suite.
    """
    reasons = []
    invocations = list(_GIT_COMMAND.finditer(script))
    if not invocations:
        return ["the manager's checkout script runs no git at all"]
    for match in invocations:
        if not script[match.end() :].startswith(HOOKLESS_GIT_PREFIX):
            excerpt = script[max(0, match.start() - 40) : match.end() + 40]
            reasons.append(
                f"a git invocation does not neutralise the workspace's hooks path: ...{excerpt}..."
            )
    if "config core.hooksPath" in script:
        reasons.append(
            "the script writes the workspace's hooks path, so the product's own hooks stop "
            "running for the developer agent's commits"
        )
    return reasons


# ── Where the story branch was cut from ──────────────────────────────────


def branch_base_mismatches(probe: dict, *, expected_contains: str) -> list[str]:
    """Why this branch was not cut from a default branch carrying that commit.

    `probe` is `story-branch-base-probe`'s payload: the commit the branch forked
    from (the compare's own merge base with the default branch) and GitHub's
    answer to whether that fork point contains `expected_contains`. Containment
    is the claim, and it is read from a comparison rather than assumed from the
    two commits being different.
    """
    reasons = []
    if not expected_contains:
        return ["nothing was named for the branch's base to contain"]
    if probe.get("contains_sha") != expected_contains:
        reasons.append(
            f"the probe asked about {probe.get('contains_sha')!r}, not {expected_contains!r}"
        )
    if not probe.get("merge_base"):
        reasons.append("the probe found no fork point for this branch")
    if probe.get("contains") is not True:
        reasons.append(
            f"{probe.get('branch')!r} forked from {probe.get('merge_base')!r}, which does not "
            f"contain {expected_contains!r} (compare status {probe.get('status')!r}): the "
            "branch was cut from something other than the default branch carrying it"
        )
    return reasons


# ── The engineering attempts of the story ────────────────────────────────

#: A Run that ended in either of these is an attempt that did not work.
FAILED_RUN_STATUSES = frozenset({"failed", "cancelled"})


def engineering_run_mismatches(runs: list[dict]) -> list[str]:
    """Why this story's engineering is not the clean single pass it has to be.

    `issue:028670f21dbd138ccd04` ended with a *done* task: the first attempt died
    on the checkout, the manager deleted the worker, the Run failed, and the
    automatic retry did the work. The task's final status therefore says nothing,
    and the durable Runs say everything.
    """
    if not runs:
        return ["the extension story ran no engineering attempt at all"]
    reasons = [
        f"engineering run {run.get('id')} for task {run.get('task_id')} ended {run.get('status')!r}"
        for run in runs
        if run.get("status") in FAILED_RUN_STATUSES
    ]
    by_task: dict[str, list[str]] = {}
    for run in runs:
        by_task.setdefault(str(run.get("task_id")), []).append(str(run.get("id")))
    reasons.extend(
        f"task {task_id} has {len(run_ids)} engineering runs ({run_ids}); a second attempt "
        "means the first one did not work"
        for task_id, run_ids in sorted(by_task.items())
        if len(run_ids) > 1
    )
    return reasons


# ── The project's own CI, for the commit that is actually deployed ───────

#: What the scheduler records the project's CI workflow under, and the branch it
#: watches it on (`services/scheduler/src/tasks/image_publication.py`).
CI_WORKFLOW = "ci.yml"
CI_BRANCH = "main"


def ci_run_mismatches(ci_runs: object, *, merge_commit_sha: str) -> list[str]:
    """Why no `ci.yml` run started for the commit this story actually deploys.

    The deployed commit is the *merge* commit, never the pull request head: no
    merge method makes the branch's new head equal the PR head, and the project's
    CI publishes images from the default branch. So the observation this asks for
    is a CI run whose head SHA is the merge commit.
    """
    if not merge_commit_sha:
        return ["the extension story's PR recorded no merge commit to run CI for"]
    if not isinstance(ci_runs, list) or not ci_runs:
        return [
            f"no {CI_WORKFLOW} run was observed for this story at all, so nothing says the "
            f"merge commit {merge_commit_sha} was built"
        ]
    matching = [run for run in ci_runs if run.get("head_sha") == merge_commit_sha]
    if not matching:
        return [
            f"no {CI_WORKFLOW} run observed for this story has head_sha {merge_commit_sha}; "
            f"observed {[run.get('head_sha') for run in ci_runs]}"
        ]
    started = [run for run in matching if run.get("id") is not None and run.get("status")]
    if not started:
        return [
            f"a {CI_WORKFLOW} run for {merge_commit_sha} is recorded but never started: {matching}"
        ]
    return []
