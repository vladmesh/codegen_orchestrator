"""Offline regressions for what only a project's *second* story can show.

Every predicate in `level1_second_story` is fed two things here: the observation
a green second story produces, and the observation the defect it exists for
produces. A predicate that answered "no reasons" to the second is exactly the
vacuous assertion this sprint keeps finding, and it is caught here rather than
by a stand run that passes while the platform is broken.

The broken fixtures are not invented. Each one is the shape the named production
run actually left behind — the checkout that hit the exec bound and was retried,
the branch cut from a stale workspace HEAD, the deploy that took the
initial-owner grant a second time — so the reason strings below are the ones a
red run would print.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from level1_second_story import (
    DEPLOY_PATH_OWNER_GRANT,
    DEPLOY_PATH_PR_POLLER,
    branch_base_mismatches,
    checkout_mismatches,
    checkout_records,
    ci_run_mismatches,
    deploy_path_mismatches,
    deploy_path_record,
    engineering_run_mismatches,
    manager_log_coverage,
    product_hook_mismatches,
    workspace_assignments,
    workspace_reuse_mismatches,
)
import pipeline_helpers
import pytest
import run_evidence

pytestmark = pytest.mark.needs_no_api_credential

BRANCH = "story/story-ea07a289"
REPO_ID = "repo-1316"
BOUND = 15


def _log(*records: dict) -> str:
    """A `docker compose logs` capture: the service prefix, then the JSON record."""
    return "".join(f"worker-manager-1  | {json.dumps(record)}\n" for record in records)


def _checkout(event: str, *, at: str, worker: str = "dev-1", branch: str = BRANCH) -> dict:
    return {"event": event, "timestamp": at, "worker_id": worker, "branch": branch}


# ── The first checkout of the story branch ───────────────────────────────


def test_a_checkout_that_took_four_seconds_is_recorded_as_four_seconds():
    """The number a reader sees, and the bound it is under."""
    log = _log(
        _checkout("checkout_branch_start", at="2026-09-19T18:50:34.000000"),
        _checkout("checkout_branch_complete", at="2026-09-19T18:50:38.120000"),
    )

    attempts = checkout_records(log, branch=BRANCH)

    assert [attempt["duration_seconds"] for attempt in attempts] == [4.12]
    assert checkout_mismatches(attempts, branch=BRANCH, bound_seconds=BOUND) == []


def test_one_repository_not_found_retry_stays_one_successful_bounded_checkout():
    log = _log(
        _checkout("checkout_branch_start", at="2026-09-19T18:50:34.000000"),
        {
            **_checkout("checkout_branch_retry", at="2026-09-19T18:50:35.000000"),
            "attempt": 1,
            "delay_seconds": 1,
        },
        _checkout("checkout_branch_complete", at="2026-09-19T18:50:50.000000"),
    )

    attempts = checkout_records(log, branch=BRANCH)

    assert len(attempts) == 1
    assert attempts[0]["retries"] == [{"attempt": 1, "delay_seconds": 1}]
    assert attempts[0]["duration_seconds"] == 16
    assert checkout_mismatches(attempts, branch=BRANCH, bound_seconds=BOUND) == []
    attempts[0]["duration_seconds"] = 17
    assert (
        "over the 16s bound" in checkout_mismatches(attempts, branch=BRANCH, bound_seconds=BOUND)[0]
    )


def test_an_unbounded_retry_claim_does_not_relax_the_checkout_assertion():
    attempts = checkout_records(
        _log(
            _checkout("checkout_branch_start", at="2026-09-19T18:50:34.000000"),
            {
                **_checkout("checkout_branch_retry", at="2026-09-19T18:50:35.000000"),
                "attempt": 1,
                "delay_seconds": 60,
            },
            _checkout("checkout_branch_complete", at="2026-09-19T18:50:50.000000"),
        ),
        branch=BRANCH,
    )
    assert (
        "invalid retry schedule"
        in checkout_mismatches(attempts, branch=BRANCH, bound_seconds=BOUND)[0]
    )


def test_the_checkout_that_hit_the_exec_bound_and_was_retried_is_refused():
    """`issue:028670f21dbd138ccd04`, as the manager's log actually recorded it.

    The first checkout starts and never ends — the exec bound killed it and the
    worker with it — and a second worker does the same job in four seconds. The
    task went on to be done, so only these two facts say anything happened.
    """
    log = _log(
        _checkout("checkout_branch_start", at="2026-09-19T18:50:34.000000"),
        _checkout("checkout_branch_start", at="2026-09-19T18:51:10.000000", worker="dev-2"),
        _checkout("checkout_branch_complete", at="2026-09-19T18:51:14.000000", worker="dev-2"),
    )

    reasons = checkout_mismatches(
        checkout_records(log, branch=BRANCH), branch=BRANCH, bound_seconds=BOUND
    )

    assert reasons == [
        f"the first checkout of {BRANCH} never reported an ending, which is what the "
        "30 s exec timeout of issue:028670f21dbd138ccd04 looks like from here",
        f"2 checkouts of {BRANCH} were run; a retried checkout means the first one did "
        "not establish the branch",
    ]


def test_a_checkout_that_failed_or_ran_long_is_refused():
    slow = _log(
        _checkout("checkout_branch_start", at="2026-09-19T18:50:34.000000"),
        _checkout("checkout_branch_complete", at="2026-09-19T18:50:57.000000"),
    )
    failed = _log(
        _checkout("checkout_branch_start", at="2026-09-19T18:50:34.000000"),
        _checkout("checkout_branch_failed", at="2026-09-19T18:50:35.000000"),
    )

    assert checkout_mismatches(
        checkout_records(slow, branch=BRANCH), branch=BRANCH, bound_seconds=BOUND
    ) == [f"the first checkout of {BRANCH} took 23.0s, over the {BOUND}s bound"]
    assert checkout_mismatches(
        checkout_records(failed, branch=BRANCH), branch=BRANCH, bound_seconds=BOUND
    ) == [f"the first checkout of {BRANCH} failed on worker dev-1"]


def test_another_story_s_checkout_is_not_this_story_s():
    """The reused workspace serves every story of the project, one branch each."""
    log = _log(
        _checkout("checkout_branch_start", at="2026-09-19T18:40:00.000000", branch="story/first"),
        _checkout(
            "checkout_branch_complete", at="2026-09-19T18:40:02.000000", branch="story/first"
        ),
    )

    assert checkout_records(log, branch=BRANCH) == []
    assert checkout_mismatches([], branch=BRANCH, bound_seconds=BOUND) == [
        f"the manager ran no checkout_branch for {BRANCH} in the log this run read"
    ]


# ── The workspace the story ran in ───────────────────────────────────────


def _assignment(worker: str, path: str, repo_id: str = REPO_ID) -> dict:
    return {
        "event": "using_scaffolded_workspace",
        "worker_id": worker,
        "repo_id": repo_id,
        "path": path,
    }


def test_both_stories_sharing_the_scaffolded_checkout_is_the_reused_workspace():
    log = _log(
        _assignment("dev-1", f"/data/workspaces/{REPO_ID}"),
        _assignment("dev-2", f"/data/workspaces/{REPO_ID}"),
        {"event": "using_ephemeral_qa_workspace", "worker_id": "qa-1", "path": "/data/ws/qa-1"},
    )

    assignments = workspace_assignments(log, repo_id=REPO_ID)

    assert [one["worker_id"] for one in assignments] == ["dev-1", "dev-2"]
    assert workspace_reuse_mismatches(assignments, repo_id=REPO_ID, worker_ids={"dev-2"}) == []


def test_the_first_story_s_assignment_alone_cannot_answer_for_the_second_story():
    """The narrowing: this story's own worker has to be in the log that is read.

    Any read of a log is a read of part of the world, so the extension worker's
    assignment being absent from what was read is the realistic way this could
    have been satisfied by the first story's line — which is exactly the shape
    of vacuity the assertion exists to avoid.
    """
    log = _log(_assignment("dev-1", f"/data/workspaces/{REPO_ID}"))

    reasons = workspace_reuse_mismatches(
        workspace_assignments(log, repo_id=REPO_ID), repo_id=REPO_ID, worker_ids={"dev-2"}
    )

    assert reasons == [
        f"worker dev-2 ran this story but was assigned no workspace for repository {REPO_ID} "
        "in the log this run read"
    ]


def test_a_second_workspace_for_the_same_repository_is_refused():
    log = _log(
        _assignment("dev-1", f"/data/workspaces/{REPO_ID}"),
        _assignment("dev-2", "/data/workspaces/qa-dev-2"),
    )

    reasons = workspace_reuse_mismatches(
        workspace_assignments(log, repo_id=REPO_ID), repo_id=REPO_ID, worker_ids={"dev-2"}
    )

    assert reasons == [
        f"the workers of repository {REPO_ID} were given 2 different workspaces: "
        f"['/data/workspaces/qa-dev-2', '/data/workspaces/{REPO_ID}']",
        f"workspace '/data/workspaces/qa-dev-2' is not the scaffolded checkout of "
        f"repository {REPO_ID}",
    ]


def test_no_workspace_assignment_at_all_and_no_named_worker_are_both_refused():
    assert workspace_reuse_mismatches([], repo_id=REPO_ID, worker_ids={"dev-2"}) == [
        f"the manager assigned no developer workspace for repository {REPO_ID} in the log "
        "this run read"
    ]
    assert workspace_reuse_mismatches(
        [_assignment("dev-2", f"/data/workspaces/{REPO_ID}")], repo_id=REPO_ID, worker_ids=set()
    ) == ["this story named no developer worker for its workspace to be judged by"]


# ── The manager's own git ────────────────────────────────────────────────


def test_the_hook_free_script_is_accepted_and_the_pre_1305_one_is_refused():
    """Card 1305: the manager's `git push -u` ran the product's `pre-push`."""
    hookless = (
        "set -e\ncd /workspace\n"
        'git -c core.hooksPath=/dev/null fetch origin "+main:refs/remotes/origin/main"\n'
        "git -c core.hooksPath=/dev/null push -u origin story/x\n"
    )
    as_it_was = "set -e\ncd /workspace\ngit fetch origin main\ngit push -u origin story/x\n"

    assert product_hook_mismatches(hookless) == []
    reasons = product_hook_mismatches(as_it_was)
    assert len(reasons) == 2
    assert all("does not neutralise the workspace's hooks path" in reason for reason in reasons)


def test_a_script_that_writes_the_workspace_hooks_path_is_refused():
    """The product's hooks have to keep running for the developer agent."""
    script = (
        "cd /workspace\n"
        "git -c core.hooksPath=/dev/null config core.hooksPath /dev/null\n"
        "git -c core.hooksPath=/dev/null push -u origin story/x\n"
    )

    assert product_hook_mismatches(script) == [
        "the script writes the workspace's hooks path, so the product's own hooks stop "
        "running for the developer agent's commits"
    ]


def test_a_script_running_no_git_proves_nothing():
    assert product_hook_mismatches("cd /workspace\necho nothing\n") == [
        "the manager's checkout script runs no git at all"
    ]


# ── Where the branch was cut from ────────────────────────────────────────

FIRST_STORY_MERGE = "a" * 40


def _base_probe(**overrides) -> dict:
    probe = {
        "branch": BRANCH,
        "base_ref": "main",
        "head_sha": "c" * 40,
        "merge_base": "b" * 40,
        "contains_sha": FIRST_STORY_MERGE,
        "status": "ahead",
        "contains": True,
    }
    probe.update(overrides)
    return probe


def test_a_branch_cut_from_a_main_carrying_the_first_story_is_accepted():
    assert branch_base_mismatches(_base_probe(), expected_contains=FIRST_STORY_MERGE) == []
    # The fork point *being* that commit is containment too.
    assert (
        branch_base_mismatches(
            _base_probe(merge_base=FIRST_STORY_MERGE, status="identical"),
            expected_contains=FIRST_STORY_MERGE,
        )
        == []
    )


def test_a_branch_cut_from_the_stale_workspace_head_is_refused():
    """Card 1305: the fork point predates the first story's merge."""
    reasons = branch_base_mismatches(
        _base_probe(status="diverged", contains=False), expected_contains=FIRST_STORY_MERGE
    )

    assert reasons == [
        f"{BRANCH!r} forked from {'b' * 40!r}, which does not contain {FIRST_STORY_MERGE!r} "
        "(compare status 'diverged'): the branch was cut from something other than the "
        "default branch carrying it"
    ]


def test_a_probe_about_another_commit_does_not_answer_this_question():
    reasons = branch_base_mismatches(
        _base_probe(contains_sha="d" * 40), expected_contains=FIRST_STORY_MERGE
    )

    assert reasons == [f"the probe asked about {'d' * 40!r}, not {FIRST_STORY_MERGE!r}"]


def test_naming_no_commit_for_the_base_to_contain_is_refused():
    assert branch_base_mismatches(_base_probe(), expected_contains="") == [
        "nothing was named for the branch's base to contain"
    ]


# ── The deploy path ──────────────────────────────────────────────────────


def _run(run_id: str, triggered_by: str) -> dict:
    return {
        "id": run_id,
        "run_metadata": {"triggered_by": triggered_by, "deploy_action": "feature"},
    }


def test_the_two_deploy_paths_are_told_apart_by_both_halves_of_their_identity():
    poller = deploy_path_record(_run("deploy-poll-1234abcd", "pr_poll"))
    grant = deploy_path_record(_run("deploy-grant-1234abcd", "users_grant_intent"))

    assert poller["path"] == DEPLOY_PATH_PR_POLLER
    assert grant["path"] == DEPLOY_PATH_OWNER_GRANT
    assert deploy_path_mismatches(poller, expected=DEPLOY_PATH_PR_POLLER) == []
    assert deploy_path_mismatches(grant, expected=DEPLOY_PATH_OWNER_GRANT) == []


def test_a_second_story_that_deployed_through_the_grant_path_is_refused():
    """The initial-owner grant belongs to a project's first story alone."""
    grant = deploy_path_record(_run("deploy-grant-1234abcd", "users_grant_intent"))

    assert deploy_path_mismatches(grant, expected=DEPLOY_PATH_PR_POLLER) == [
        "deploy run 'deploy-grant-1234abcd' triggered_by='users_grant_intent' is owner_grant, "
        "not pr_poller (which mints deploy-poll-… and stamps triggered_by='pr_poll')"
    ]


def test_a_run_whose_halves_disagree_is_classified_as_no_path_at_all():
    """One half alone is a coincidence away from being wrong."""
    mixed = deploy_path_record(_run("deploy-poll-1234abcd", "users_grant_intent"))

    assert mixed["path"] is None
    assert deploy_path_mismatches(mixed, expected=DEPLOY_PATH_PR_POLLER) == [
        "deploy run 'deploy-poll-1234abcd' triggered_by='users_grant_intent' is no recognised "
        "path, not pr_poller (which mints deploy-poll-… and stamps triggered_by='pr_poll')"
    ]


def test_an_unknown_expected_path_is_a_programming_error_not_a_pass():
    with pytest.raises(ValueError, match="unknown deploy path"):
        deploy_path_mismatches(deploy_path_record(_run("deploy-poll-1", "pr_poll")), expected="any")


# ── The engineering attempts ─────────────────────────────────────────────


def test_one_completed_attempt_per_task_is_accepted():
    assert (
        engineering_run_mismatches([{"id": "eng-1", "task_id": "task-1", "status": "completed"}])
        == []
    )


def test_a_failed_attempt_and_its_retry_are_both_reported():
    """The shape `issue:028670f21dbd138ccd04` leaves behind: a done task, two Runs."""
    reasons = engineering_run_mismatches(
        [
            {"id": "eng-1", "task_id": "task-1", "status": "failed"},
            {"id": "eng-2", "task_id": "task-1", "status": "completed"},
        ]
    )

    assert reasons == [
        "engineering run eng-1 for task task-1 ended 'failed'",
        "task task-1 has 2 engineering runs (['eng-1', 'eng-2']); a second attempt means the "
        "first one did not work",
    ]


def test_a_cancelled_attempt_is_refused_and_no_attempt_at_all_is_too():
    assert engineering_run_mismatches(
        [{"id": "eng-1", "task_id": "task-1", "status": "cancelled"}]
    ) == ["engineering run eng-1 for task task-1 ended 'cancelled'"]
    assert engineering_run_mismatches([]) == [
        "the extension story ran no engineering attempt at all"
    ]


# ── The project's own CI, for the merge commit ───────────────────────────

MERGE_COMMIT = "e" * 40


def test_a_ci_run_for_the_merge_commit_is_accepted():
    runs = [
        {
            "id": 34162226616,
            "status": "completed",
            "conclusion": "success",
            "head_sha": MERGE_COMMIT,
        }
    ]

    assert ci_run_mismatches(runs, merge_commit_sha=MERGE_COMMIT) == []


def test_a_ci_run_for_the_pull_request_head_does_not_answer_for_the_merge_commit():
    """No merge method makes the branch's new head equal the PR head."""
    runs = [{"id": 1, "status": "completed", "conclusion": "success", "head_sha": "f" * 40}]

    assert ci_run_mismatches(runs, merge_commit_sha=MERGE_COMMIT) == [
        f"no ci.yml run observed for this story has head_sha {MERGE_COMMIT}; observed {['f' * 40]}"
    ]


def test_no_observed_ci_run_and_no_merge_commit_are_both_refused():
    assert ci_run_mismatches([], merge_commit_sha=MERGE_COMMIT) == [
        f"no ci.yml run was observed for this story at all, so nothing says the merge commit "
        f"{MERGE_COMMIT} was built"
    ]
    assert ci_run_mismatches(None, merge_commit_sha=MERGE_COMMIT)
    assert ci_run_mismatches([], merge_commit_sha="") == [
        "the extension story's PR recorded no merge commit to run CI for"
    ]


# ── What the evidence artifact says about the second story ───────────────


def test_the_artifact_section_carries_the_checkout_duration_as_a_number():
    """AC 3's "a number a reader can see": it is in the artifact, not implied."""
    ctx = {
        "level1_extension": {
            "story_id": "story-ea07a289",
            "task_ids": ["task-1"],
            "task_status": "done",
            "first_checkout": [
                {
                    "branch": BRANCH,
                    "worker_id": "dev-2",
                    "started_at": "2026-09-19T18:50:34.000000",
                    "completed_at": "2026-09-19T18:50:38.120000",
                    "duration_seconds": 4.12,
                    "failed": False,
                }
            ],
            "deploy_path": {"run_id": "deploy-poll-1", "path": DEPLOY_PATH_PR_POLLER},
        }
    }

    section = run_evidence.second_story(ctx)

    assert section["ran"]["status"] == run_evidence.CaptureStatus.CAPTURED.value
    assert section["first_checkout"]["value"][0]["duration_seconds"] == 4.12
    assert section["deploy_path"]["value"]["path"] == DEPLOY_PATH_PR_POLLER


def test_an_unread_second_story_fact_says_why_rather_than_being_absent():
    ctx = {
        "level1_extension": {
            "story_id": "story-ea07a289",
            "first_checkout_error": "docker compose logs worker-manager exited 1",
        }
    }

    section = run_evidence.second_story(ctx)

    assert section["first_checkout"]["status"] == run_evidence.CaptureStatus.MISSED.value
    assert section["first_checkout"]["reason"] == "docker compose logs worker-manager exited 1"


def test_a_run_with_no_second_story_says_so():
    section = run_evidence.second_story({})

    assert section["ran"]["status"] == run_evidence.CaptureStatus.MISSED.value
    assert section["ran"]["reason"] == "this run ran no second story"


# ── The read of the manager's log itself ─────────────────────────────────

#: The manager's own rendering, copied from stand-e2e run 35475905032's
#: `suite-services.log`. The worker-manager process never calls
#: `shared.log_config.setup_logging`, so the container's `LOG_FORMAT=json` never
#: reaches structlog and every line it writes looks like this.
_CONSOLE_CHECKOUT = (
    "worker-manager-1  | 2026-09-19 23:59:53 [info     ] checkout_branch_start"
    "          branch={branch} correlation_id=491f2d40-423e-4169-9dc4-22e3db3a930c "
    "request_id=7c8954f1 worker_id={worker}\n"
    "worker-manager-1  | 2026-09-19 23:59:58 [info     ] checkout_branch_complete"
    "       branch={branch} correlation_id=491f2d40-423e-4169-9dc4-22e3db3a930c "
    "request_id=7c8954f1 worker_id={worker}\n"
)
_CONSOLE_ASSIGNMENT = (
    "worker-manager-1  | 2026-09-19 23:59:52 [info     ] using_scaffolded_workspace"
    "     correlation_id=491f2d40 path=/data/workspaces/{repo} repo_id={repo} "
    "request_id=7c8954f1 worker_id={worker}\n"
)
#: What the manager writes between the interesting lines. Run 35475905032's
#: manager wrote roughly two hundred lines in total; six thousand is well past
#: the `--tail=5000` the read used to carry.
_CHATTER = 'worker-manager-1  | INFO:     127.0.0.1:47484 - "GET /health HTTP/1.1" 200 OK\n'


def _console_log(*, branch: str = BRANCH, worker: str = "dev-2", repo: str = REPO_ID) -> str:
    return _CONSOLE_ASSIGNMENT.format(repo=repo, worker=worker) + _CONSOLE_CHECKOUT.format(
        branch=branch, worker=worker
    )


def test_the_manager_s_console_rendered_checkout_is_read_as_a_record():
    """Run 35475905032's own lines, read as the checkout they are.

    The regression: this capture used to yield nothing at all, because the
    reader knew only `JSONRenderer` output and the manager writes structlog's
    default console rendering. An empty answer then looked like a manager that
    had run no checkout.
    """
    log = _console_log()

    attempts = checkout_records(log, branch=BRANCH)

    assert [attempt["worker_id"] for attempt in attempts] == ["dev-2"]
    assert attempts[0]["duration_seconds"] == 5.0
    assert checkout_mismatches(attempts, branch=BRANCH, bound_seconds=BOUND) == []
    assert workspace_assignments(log, repo_id=REPO_ID) == [
        {"worker_id": "dev-2", "repo_id": REPO_ID, "path": f"/data/workspaces/{REPO_ID}"}
    ]


def test_both_renderings_are_read_out_of_one_capture():
    """A capture holds whatever each service was configured to write."""
    log = _console_log() + _log(
        _checkout("checkout_branch_start", at="2026-09-19T18:50:34.000000", branch="story/other"),
    )

    assert [one["branch"] for one in checkout_records(log, branch=BRANCH)] == [BRANCH]
    assert [one["branch"] for one in checkout_records(log, branch="story/other")] == ["story/other"]


def test_a_capture_with_no_record_in_it_is_not_an_empty_list_of_checkouts():
    """The two empty answers, told apart by what the capture holds."""
    unreadable = _CHATTER * 10

    assert manager_log_coverage(unreadable)["records"] == 0
    assert manager_log_coverage(_console_log())["records"] == 3
    assert manager_log_coverage(_console_log())["first_record_at"] == "2026-09-19T23:59:52"


def _fake_compose_logs(monkeypatch, *, stdout: str, returncode: int = 0) -> list[list[str]]:
    """Stand in for the one `docker compose logs` the reader runs, and record it."""
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(pipeline_helpers.subprocess, "run", fake_run)
    return calls


def _reader_ctx() -> dict:
    return {"story_id": BRANCH.removeprefix("story/"), "repo_id": REPO_ID}


def test_the_reader_finds_a_checkout_that_sits_beyond_the_old_line_bound(monkeypatch):
    """Six thousand lines of chatter before the checkout, and it is still read.

    The old read asked for the last 5000 lines, so a run chatty enough to bury
    its own checkout under that many lines reported no checkout at all. The
    read is now bounded by the container's start instead, which is why the
    command it runs carries no `--tail` for a chattier run to outgrow.
    """
    ctx = _reader_ctx()
    calls = _fake_compose_logs(monkeypatch, stdout=_CHATTER * 6000 + _console_log())

    pipeline_helpers.record_first_checkout(ctx)

    assert ctx["first_checkout_error"] is None
    assert {attempt["worker_id"] for attempt in ctx["first_checkout"]} == {"dev-2"}
    assert ctx["workspace_assignments"] == [
        {"worker_id": "dev-2", "repo_id": REPO_ID, "path": f"/data/workspaces/{REPO_ID}"}
    ]
    assert calls == [["docker", "compose", "logs", "--no-color", "worker-manager"]]
    assert not [argument for argument in calls[0] if argument.startswith("--tail")]


def test_a_manager_that_logged_no_checkout_is_reported_as_absent(monkeypatch):
    """Absent, not unreadable — and the run still fails on it."""
    ctx = _reader_ctx()
    _fake_compose_logs(monkeypatch, stdout=_console_log(branch="story/somebody-else"))

    pipeline_helpers.record_first_checkout(ctx)

    assert ctx["first_checkout_error"] is None
    assert ctx["first_checkout"] == []
    assert ctx["manager_log_read"]["records"] == 3
    section = run_evidence.second_story({"level1_extension": ctx})
    assert section["first_checkout"]["status"] == run_evidence.CaptureStatus.CAPTURED.value
    assert section["first_checkout"]["value"] == []
    assert section["manager_log_read"]["status"] == run_evidence.CaptureStatus.CAPTURED.value
    assert section["manager_log_read"]["value"]["bound"] == pipeline_helpers.MANAGER_LOG_BOUND
    # Fail-closed: an absent checkout is still a red run that names why.
    assert checkout_mismatches(ctx["first_checkout"], branch=BRANCH, bound_seconds=BOUND) == [
        f"the manager ran no checkout_branch for {BRANCH} in the log this run read"
    ]


def test_a_log_that_yields_no_record_is_reported_as_unreadable(monkeypatch):
    """Nothing parsed out of the capture says nothing about the manager."""
    ctx = _reader_ctx()
    _fake_compose_logs(monkeypatch, stdout=_CHATTER * 10)

    pipeline_helpers.record_first_checkout(ctx)

    assert "first_checkout" not in ctx
    assert "cannot be told" in ctx["first_checkout_error"]
    assert ctx["manager_log_read"]["records"] == 0
    section = run_evidence.second_story({"level1_extension": ctx})
    assert section["first_checkout"]["status"] == run_evidence.CaptureStatus.MISSED.value
    assert section["first_checkout"]["reason"] == ctx["first_checkout_error"]


def test_a_read_that_failed_is_reported_as_unreadable(monkeypatch):
    """The other unreadable: compose itself did not answer."""
    ctx = _reader_ctx()
    _fake_compose_logs(monkeypatch, stdout="", returncode=1)

    pipeline_helpers.record_first_checkout(ctx)

    assert "first_checkout" not in ctx
    assert ctx["first_checkout_error"] == "docker compose logs worker-manager exited 1"
    section = run_evidence.second_story({"level1_extension": ctx})
    assert section["first_checkout"]["status"] == run_evidence.CaptureStatus.MISSED.value
    assert section["manager_log_read"]["status"] == run_evidence.CaptureStatus.MISSED.value
    assert section["manager_log_read"]["reason"] == ctx["first_checkout_error"]
