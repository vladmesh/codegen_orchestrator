"""A Claude turn keeps its provider-reported cost through the result completion barrier.

The agent here is a real process standing in for the Claude CLI. It commits its work in a
real checkout, posts its result to the wrapper's real HTTP endpoint and then prints the
final-result document captured from the pinned Claude Code (see ``tests/fixtures``). The
wrapper then pushes the commit to a real bare origin and reads it back. Nothing between
the endpoint and the published result is mocked.
"""

import asyncio
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from worker_wrapper import wrapper as wrapper_module
from worker_wrapper.config import WorkerWrapperConfig
from worker_wrapper.observability import _extract_claude_evidence
from worker_wrapper.wrapper import WorkerWrapper

from shared.contracts.dto.engineering_attempt import EngineeringAttemptLedgerInput
from shared.contracts.queues.worker_result import (
    WorkerCompletedResult,
    WorkerFailedResult,
    parse_worker_result,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "claude_code_2.1.278_result.json"
BRANCH = "story/story-1"
FENCE_LOCKS = ("index.lock", "HEAD.lock", f"refs/heads/{BRANCH}.lock")

# What the stand-in CLI does. It commits the task's work and reports it, like a real
# developer turn, and then does what the scenario says before it ends its turn.
AGENT = r"""
import json, os, subprocess, sys, time, urllib.request

scenario, port, fixture, attempts_log = sys.argv[1:5]
IDENTITY = ["-c", "user.name=agent", "-c", "user.email=agent@example.com"]

def git(*args):
    done = subprocess.run(["git", *IDENTITY, *args], capture_output=True, text=True)
    return done.returncode, done.stdout.strip()

with open("work.py", "w") as f:
    f.write("def work():\n    return 1\n")
git("add", "work.py")
git("commit", "-m", "feat: the task")
git("push", "origin", f"HEAD:refs/heads/{os.environ['BRANCH']}")
_, sha = git("rev-parse", "HEAD")
request = urllib.request.Request(
    f"http://127.0.0.1:{port}/result",
    data=json.dumps({"success": True, "commit": sha, "summary": "did the task"}).encode(),
    headers={"Content-Type": "application/json"},
)
urllib.request.urlopen(request).read()
# The pinned CLI took 0.9-1.8 s from its result POST to its exit in real captures.
time.sleep(1)

if scenario == "late-git":
    # Everything an agent could naturally try once it has reported.
    with open("late.py", "w") as f:
        f.write("LATE = True\n")
    attempts = {
        "commit": git("commit", "-am", "late", "--allow-empty")[0],
        "add_and_commit": (git("add", "late.py")[0], git("commit", "-m", "late")[0]),
        "amend": git("commit", "--amend", "-m", "rewritten")[0],
        "reset": git("reset", "--hard", "HEAD~1")[0],
        "branch_switch": git("checkout", "-b", "escape")[0],
        "update_ref": git("update-ref", f"refs/heads/{os.environ['BRANCH']}", "HEAD~1")[0],
        "push_ancestor": git(
            "push", "--force", "origin", f"HEAD~1:refs/heads/{os.environ['BRANCH']}"
        )[0],
    }
    with open(attempts_log, "w") as f:
        json.dump(attempts, f)
elif scenario == "plumbing-push":
    # A deliberate bypass: a commit object with no ref, pushed by hand.
    _, tree = git("rev-parse", "HEAD^{tree}")
    _, orphan = git("commit-tree", tree, "-p", "HEAD", "-m", "smuggled")
    pushed = git("push", "origin", f"{orphan}:refs/heads/{os.environ['BRANCH']}")[0]
    with open(attempts_log, "w") as f:
        json.dump({"plumbing_push": pushed, "orphan": orphan}, f)
elif scenario == "never-ends":
    time.sleep(60)
elif scenario == "unparsable":
    sys.stdout.write("Done.\n")
    sys.exit(0)

with open(fixture) as f:
    sys.stdout.write(f.read())
"""


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60, check=True
    )
    return done.stdout.strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A developer checkout on its story branch, with a bare origin it can push to."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", BRANCH, str(origin))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    git(workspace, "init", "-b", BRANCH)
    git(workspace, "config", "user.name", "seed")
    git(workspace, "config", "user.email", "seed@example.com")
    (workspace / "README.md").write_text("product\n")
    git(workspace, "add", "README.md")
    git(workspace, "commit", "-m", "seed")
    git(workspace, "remote", "add", "origin", str(origin))
    git(workspace, "push", "origin", f"HEAD:refs/heads/{BRANCH}")
    monkeypatch.setattr("worker_wrapper.wrapper.WORKSPACE_DIR", str(workspace))
    return workspace, origin


def _wrapper(tmp_path, **overrides) -> tuple[WorkerWrapper, MagicMock]:
    config = {
        "broker_url": "http://worker-broker:8001",
        "broker_token": "x" * 43,
        "worker_id": "dev-grace-1",
        "agent_type": "claude",
        "subprocess_timeout_seconds": 60,
        "http_server_port": 0,
        "transcript_dir": str(tmp_path / "transcripts"),
    }
    config.update(overrides)
    broker = MagicMock()
    broker.get_session = AsyncMock(return_value=None)
    broker.set_session = AsyncMock()
    broker.clear_session = AsyncMock()
    broker.update_status = AsyncMock()
    broker.submit_output = AsyncMock()
    broker.compose = AsyncMock()
    return WorkerWrapper(config=WorkerWrapperConfig(**config), broker_client=broker), broker


async def _run_turn(wrapper: WorkerWrapper, tmp_path: Path, scenario: str, *, grace: float):
    """One whole turn, with the stand-in CLI in place of `claude` and nothing else faked."""
    agent = tmp_path / "agent.py"
    agent.write_text(AGENT, encoding="utf-8")
    real_exec = asyncio.create_subprocess_exec

    async def spawn_standin(*_cmd, **kwargs):
        kwargs["env"] = {**kwargs["env"], "BRANCH": BRANCH}
        return await real_exec(
            sys.executable,
            str(agent),
            scenario,
            str(wrapper._http_server.port),
            str(FIXTURE),
            str(tmp_path / "attempts.json"),
            cwd=wrapper_module.WORKSPACE_DIR,
            **kwargs,
        )

    with (
        patch.object(wrapper, "_prepare_workspace", AsyncMock()),
        patch.object(wrapper, "_check_workspace_ready", return_value=(True, "ready")),
        patch.object(wrapper, "_fix_venv_paths"),
        patch.object(wrapper, "_install_compose_proxy"),
        patch("worker_wrapper.wrapper.asyncio.create_subprocess_exec", spawn_standin),
        patch("worker_wrapper.wrapper.RESULT_TURN_END_GRACE_SECONDS", grace),
        patch("worker_wrapper.wrapper.RESULT_STOP_GRACE_SECONDS", 0),
        patch("worker_wrapper.wrapper.OLD_TASKS_DIR", str(tmp_path / "old_tasks")),
    ):
        await asyncio.wait_for(
            wrapper.process_message(
                "1-0", {"request_id": "req-1", "prompt": "do the task", "branch": BRANCH}
            ),
            timeout=30,
        )
    return wrapper.broker.submit_output.await_args[0][1]


def _fixture_evidence():
    return _extract_claude_evidence(FIXTURE.read_text(encoding="utf-8"))["claude_evidence"]


def _no_fence_left(workspace: Path) -> bool:
    return not any((workspace / ".git" / lock).exists() for lock in FENCE_LOCKS)


def test_the_fixture_is_the_pinned_cli_result_document():
    """The captured document parses whole through the one Claude evidence path."""
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    evidence = _fixture_evidence()

    assert document["type"] == "result"
    assert document["total_cost_usd"] == 0.056448
    assert evidence.cost_microusd == 56_448
    assert evidence.model == "claude-sonnet-5"
    assert (evidence.input_tokens, evidence.output_tokens) == (6, 340)
    assert (evidence.cache_read_tokens, evidence.cache_write_tokens) == (73_740, 9_572)


@pytest.mark.asyncio
async def test_a_turn_that_ends_inside_the_grace_keeps_its_provider_cost(tmp_path, checkout):
    """AC1: the result is published with the cost document the CLI printed after it."""
    workspace, origin = checkout
    wrapper, _ = _wrapper(tmp_path)

    published = await _run_turn(wrapper, tmp_path, "ends", grace=10)

    head = git(workspace, "rev-parse", "HEAD")
    assert isinstance(published, WorkerCompletedResult)
    assert published.commit_sha == head
    assert git(origin, "rev-parse", BRANCH) == head
    assert published.claude_evidence == _fixture_evidence()
    assert published.claude_evidence.cost_microusd == 56_448
    ledger = EngineeringAttemptLedgerInput.model_validate(
        {"claude_evidence": parse_worker_result(published.model_dump(mode="json")).claude_evidence}
    )
    assert ledger.cost_source == "provider_reported"
    assert ledger.cost_microusd == 56_448
    assert _no_fence_left(workspace)


@pytest.mark.asyncio
async def test_late_git_after_the_result_cannot_change_what_the_turn_reports(tmp_path, checkout):
    """AC2: a commit, amend, reset, branch switch or push after the result changes nothing."""
    workspace, origin = checkout
    wrapper, _ = _wrapper(tmp_path)

    published = await _run_turn(wrapper, tmp_path, "late-git", grace=10)

    attempts = json.loads((tmp_path / "attempts.json").read_text())
    assert attempts["commit"] != 0
    assert attempts["add_and_commit"][1] != 0
    assert attempts["amend"] != 0
    assert attempts["reset"] != 0
    assert attempts["branch_switch"] != 0
    assert attempts["update_ref"] != 0
    reported = git(workspace, "rev-parse", "HEAD")
    assert git(workspace, "log", "-1", "--format=%s", reported) == "feat: the task"
    assert git(workspace, "rev-parse", "--abbrev-ref", "HEAD") == BRANCH
    # The agent's push moved origin back to an ancestor, and the wrapper's own push
    # still leaves the story branch on the reported commit.
    assert attempts["push_ancestor"] == 0
    assert git(origin, "rev-parse", BRANCH) == reported
    assert isinstance(published, WorkerCompletedResult)
    assert published.commit_sha == reported
    assert published.claude_evidence == _fixture_evidence()
    assert _no_fence_left(workspace)


@pytest.mark.asyncio
async def test_a_deliberately_smuggled_commit_is_refused_not_published(tmp_path, checkout):
    """A commit object pushed by hand around the fence never becomes the reported SHA."""
    workspace, origin = checkout
    wrapper, _ = _wrapper(tmp_path)

    published = await _run_turn(wrapper, tmp_path, "plumbing-push", grace=10)

    attempts = json.loads((tmp_path / "attempts.json").read_text())
    assert attempts["plumbing_push"] == 0
    assert isinstance(published, WorkerFailedResult)
    assert "could not be pushed" in published.error
    assert git(origin, "rev-parse", BRANCH) == attempts["orphan"]
    assert git(workspace, "rev-parse", "HEAD") != attempts["orphan"]
    assert _no_fence_left(workspace)


@pytest.mark.asyncio
async def test_a_cli_that_does_not_end_in_the_grace_is_stopped_with_unknown_cost(
    tmp_path, checkout
):
    """AC3: past the grace the process group is stopped as before, and cost is unknown."""
    workspace, origin = checkout
    wrapper, _ = _wrapper(tmp_path)

    published = await _run_turn(wrapper, tmp_path, "never-ends", grace=0.5)

    head = git(workspace, "rev-parse", "HEAD")
    assert isinstance(published, WorkerCompletedResult)
    assert published.commit_sha == head
    assert git(origin, "rev-parse", BRANCH) == head
    assert published.claude_evidence is None
    assert _no_fence_left(workspace)


@pytest.mark.asyncio
async def test_a_turn_end_without_a_result_document_keeps_cost_unknown(tmp_path, checkout):
    """AC3: output that is not one final-result document is no provider figure."""
    wrapper, _ = _wrapper(tmp_path)

    published = await _run_turn(wrapper, tmp_path, "unparsable", grace=10)

    assert isinstance(published, WorkerCompletedResult)
    assert published.claude_evidence is None


@pytest.mark.asyncio
async def test_a_fence_that_cannot_be_taken_stops_the_agent_at_once(tmp_path, checkout):
    """A git lock held by someone else means no grace, and it is not the wrapper's to remove."""
    workspace, _ = checkout
    wrapper, _ = _wrapper(tmp_path)
    foreign = workspace / ".git" / "HEAD.lock"
    foreign.write_text("another git process\n")
    wrapper._result_event = asyncio.Event()
    wrapper._result_event.set()
    proc = MagicMock()
    proc.pid = 4321
    proc.returncode = -9
    killed = asyncio.Event()

    async def hang():
        await killed.wait()
        return b"", b""

    async def wait():
        await killed.wait()
        return -9

    proc.communicate = hang
    proc.wait = wait
    signals: list[int] = []

    def kill_group(_pid, sent):
        signals.append(sent)
        killed.set()

    with (
        patch("worker_wrapper.wrapper.RESULT_TURN_END_GRACE_SECONDS", 30),
        patch("worker_wrapper.wrapper.RESULT_STOP_GRACE_SECONDS", 0),
        patch("worker_wrapper.wrapper.os.killpg", side_effect=kill_group),
    ):
        outcome = await asyncio.wait_for(wrapper._finish_agent_process(proc), timeout=5)

    assert outcome == ("", "", False, True)
    assert signals
    assert wrapper._effort_metrics == {}
    assert foreign.read_text() == "another git process\n"
    assert not (workspace / ".git" / "index.lock").exists()


def test_other_agents_are_still_stopped_at_the_barrier(tmp_path, checkout):
    """Only Claude prints a cost document worth waiting for; the others get no grace."""
    workspace, _ = checkout
    wrapper, _ = _wrapper(tmp_path, agent_type="codex")

    with wrapper._result_fence() as fenced:
        assert fenced is False
        assert _no_fence_left(workspace)


@pytest.mark.asyncio
async def test_auto_resume_evidence_replaces_the_first_runs(tmp_path):
    """The resumed CLI reports the session's cumulative cost, so its document is the turn's."""
    wrapper, broker = _wrapper(tmp_path)
    broker.get_session.return_value = "resume-session"
    wrapper._result_event = asyncio.Event()
    wrapper._effort_metrics = {"claude_evidence": "from the first run"}
    resumed = MagicMock()
    resumed.pid = 4321
    resumed.returncode = 0
    resumed.communicate = AsyncMock(return_value=(FIXTURE.read_bytes(), b""))
    resumed.wait = AsyncMock(return_value=0)

    with (
        patch(
            "worker_wrapper.wrapper.asyncio.create_subprocess_exec",
            AsyncMock(return_value=resumed),
        ),
        patch("worker_wrapper.wrapper.os.killpg", side_effect=ProcessLookupError),
    ):
        assert await wrapper._attempt_auto_resume({"request_id": "req-1"}) is True

    assert wrapper._effort_metrics == {"claude_evidence": _fixture_evidence()}


@pytest.mark.asyncio
async def test_a_qa_claude_executor_delivers_its_evidence_in_the_output_payload(tmp_path):
    """AC5: a QA executor is never cut off by a result, so its CLI prints its document."""
    wrapper, broker = _wrapper(tmp_path, worker_type="qa")
    qa_agent = MagicMock()
    qa_agent.pid = 4321
    qa_agent.returncode = 0
    qa_agent.communicate = AsyncMock(return_value=(FIXTURE.read_bytes(), b""))
    qa_agent.wait = AsyncMock(return_value=0)

    with (
        patch("worker_wrapper.wrapper.WORKSPACE_DIR", str(tmp_path)),
        patch(
            "worker_wrapper.wrapper.asyncio.create_subprocess_exec",
            AsyncMock(return_value=qa_agent),
        ),
        patch("worker_wrapper.wrapper.os.killpg", side_effect=ProcessLookupError),
    ):
        await wrapper.process_message("1-0", {"request_id": "qa-1", "prompt": "explore"})

    payload = broker.submit_output.await_args[0][1].model_dump(mode="json")
    assert payload["claude_evidence"] == _fixture_evidence().model_dump(mode="json")
    assert payload["claude_evidence"]["cost_microusd"] == 56_448
