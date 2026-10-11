"""Owned developer intake and strict publication with controlled native Git replies."""

import asyncio
import json
import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest
from worker_wrapper.config import WorkerWrapperConfig
from worker_wrapper.wrapper import WorkerWrapper

from shared.contracts.queues.worker_result import WorkerResultStatus

CLAIM = "b0009d8eb5edaef64a5809978b01f8c0bb774970"
HEAD = "b0009d896fd15812333b5d084d2af76039f576df"
STALE = "c" * 40
BRANCH = "story/story-ea4aea41"
TURN = {"branch": BRANCH, "attempt_id": "eng-128be0fc1fb9", "prompt": "finish task"}


class GitReplies:
    def __init__(self):
        self.head = HEAD
        self.branch = BRANCH
        self.remote = None
        self.push_code = 0
        self.readback_matches = True
        self.unreadable = False
        self.paths = "services/backend/src/app.py"
        self.calls = []

    def __call__(self, argv, **kwargs):
        args = argv[3:]
        self.calls.append(args)
        if self.unreadable:
            raise OSError("Git unavailable")
        code, out, err = 0, "", ""
        match args:
            case ["check-ref-format", _]:
                pass
            case ["symbolic-ref", "--short", "HEAD"]:
                out = self.branch
            case ["rev-parse", "--verify", "--end-of-options", revision]:
                named = revision.removesuffix("^{commit}")
                if named in (HEAD, HEAD[:7], "HEAD"):
                    out = self.head if named == "HEAD" else HEAD
                elif named == STALE:
                    out = STALE
                else:
                    code, err = 128, "object missing https://user:secret@github.com/repo"
            case ["rev-parse", "--verify", "HEAD^{commit}"]:
                out = self.head
            case ["rev-list", "--parents", "-n", "1", sha]:
                out = f"{sha} {STALE}"
            case ["diff", "--name-only", _, _]:
                out = self.paths
            case ["ls-remote", "--exit-code", "--heads", "origin", ref]:
                if self.remote:
                    out = f"{self.remote}\t{ref}"
                else:
                    code = 2
            case ["push", "origin", refspec]:
                code, err = self.push_code, "controlled push refusal" if self.push_code else ""
                if code == 0 and self.readback_matches:
                    self.remote = refspec.split(":")[0]
            case _:
                raise AssertionError(args)
        return subprocess.CompletedProcess(argv, code, out + "\n", err)


@pytest.fixture
def owned_turn(monkeypatch):
    git = GitReplies()
    monkeypatch.setattr("shared.commit_publication.subprocess.run", git)
    wrapper = WorkerWrapper(
        WorkerWrapperConfig(
            broker_url="http://broker.test",
            broker_token="x" * 43,
            worker_id="test-worker",
            agent_type="codex",
        ),
        broker_client=MagicMock(),
    )
    wrapper.broker.update_status = AsyncMock()
    wrapper.broker.submit_output = AsyncMock()
    wrapper.broker.compose = AsyncMock()
    monkeypatch.setattr(wrapper, "_git_auth_preflight", lambda: True)
    monkeypatch.setattr(wrapper, "_prepare_workspace", AsyncMock())
    monkeypatch.setattr(wrapper, "_check_workspace_ready", lambda: (True, "ready"))
    monkeypatch.setattr(wrapper, "_fix_venv_paths", lambda: None)
    monkeypatch.setattr(wrapper, "_install_compose_proxy", lambda: None)
    monkeypatch.setattr(wrapper, "_read_worker_report", lambda: None)
    monkeypatch.setattr(wrapper, "_archive_task", lambda *args: None)
    monkeypatch.setattr("worker_wrapper.http_server.ResultHttpServer.start", AsyncMock())
    monkeypatch.setattr("worker_wrapper.http_server.ResultHttpServer.stop", AsyncMock())
    publish = wrapper._publish_result

    async def publish_without_agent_error(lease, data, error, status, report):
        # _run_turn normally catches subprocess failures. Assertions in this
        # controlled agent must still fail the test before result publication.
        assert error is None, error
        return await publish(lease, data, error, status, report)

    monkeypatch.setattr(wrapper, "_publish_result", publish_without_agent_error)
    return wrapper, git


async def post(wrapper, commit=HEAD, **payload):
    body = {"success": True, "commit": commit, "summary": 'done "safely"\n'}
    body.update(payload)
    return await wrapper._http_server._handle_result(json.dumps(body).encode())


@pytest.mark.parametrize(
    "invalid,condition,reason",
    [
        (CLAIM, None, "object_missing"),
        (STALE, None, "head_changed"),
        (HEAD, "wrong_branch", "wrong_branch"),
        (HEAD, "unreadable", "inspection_failed"),
        (HEAD, "injected", "injected_paths"),
    ],
)
async def test_invalid_claim_can_be_corrected_in_same_owned_turn(
    owned_turn,
    monkeypatch,
    invalid,
    condition,
    reason,
):
    wrapper, git = owned_turn

    async def agent(data):
        if condition == "wrong_branch":
            git.branch = "story/foreign"
        if condition == "unreadable":
            git.unreadable = True
        if condition == "injected":
            git.paths = "WORKER_INSTRUCTIONS.md"
        status, body = await post(wrapper, invalid)
        assert status == 400
        assert body["publication"]["failure"] == reason
        assert "secret" not in json.dumps(body)
        assert not wrapper._result_event.is_set()
        assert wrapper._buffered_result is None
        wrapper.broker.submit_output.assert_not_awaited()
        assert not any(args[0] == "push" for args in git.calls)
        git.branch, git.unreadable = BRANCH, False
        git.paths = "services/backend/src/app.py"
        from worker_wrapper.report_completed import completed_payload

        # Use the same native producer as the installed completion command;
        # only its Git process is replaced by controlled replies in this layer.
        payload = completed_payload('done "safely"\n')
        status, _ = await wrapper._http_server._handle_result(payload)
        assert status == 200
        assert wrapper._result_event.is_set()
        assert wrapper._buffered_result.commit_sha == HEAD
        status, _ = await post(wrapper, STALE)
        assert status == 409

    monkeypatch.setattr(wrapper, "execute_agent", agent)
    await wrapper._run_turn("lease-003", TURN)
    submitted = wrapper.broker.submit_output.await_args.args[1]
    assert submitted.status == WorkerResultStatus.COMPLETED
    assert submitted.commit_sha == git.remote == HEAD
    wrapper.broker.submit_output.assert_awaited_once()


@pytest.mark.parametrize("claim", [HEAD[:7], "HEAD"])
async def test_acceptance_freezes_resolved_identity_before_later_head_change(
    owned_turn,
    monkeypatch,
    claim,
):
    wrapper, git = owned_turn

    async def agent(data):
        assert (await post(wrapper, claim))[0] == 200
        assert wrapper._buffered_result.commit_sha == HEAD
        git.head = STALE

    monkeypatch.setattr(wrapper, "execute_agent", agent)
    await wrapper._run_turn("lease-003", TURN)
    submitted = wrapper.broker.submit_output.await_args.args[1]
    assert submitted.status == WorkerResultStatus.FAILED
    assert submitted.publication.failure == "head_changed"
    assert submitted.publication.commit_sha == HEAD
    assert submitted.worker_report == 'done "safely"\n'
    assert not any(args[0] == "push" for args in git.calls)


@pytest.mark.parametrize(
    "push_code,matches,reason",
    [
        (1, True, "push_refused"),
        (0, False, "readback_mismatch"),
    ],
)
async def test_http_acceptance_does_not_hide_final_publication_refusal(
    owned_turn,
    monkeypatch,
    push_code,
    matches,
    reason,
):
    wrapper, git = owned_turn
    git.push_code, git.readback_matches = push_code, matches

    async def agent(data):
        assert (await post(wrapper))[0] == 200

    monkeypatch.setattr(wrapper, "execute_agent", agent)
    await wrapper._run_turn("lease-003", TURN)
    result = wrapper.broker.submit_output.await_args.args[1]
    assert result.status == WorkerResultStatus.FAILED
    assert result.publication.failure == reason
    assert result.publication.attempt_id == TURN["attempt_id"]
    assert result.publication.worker_id == wrapper.config.worker_id
    assert [args for args in git.calls if args[0] == "push"] == [
        ["push", "origin", f"{HEAD}:refs/heads/{BRANCH}"]
    ]


@pytest.mark.parametrize("qa,success", [(False, False), (True, True), (True, False)])
async def test_failure_and_qa_do_not_require_checkout(owned_turn, monkeypatch, qa, success):
    wrapper, git = owned_turn
    if qa:
        wrapper.config.worker_type = "qa"
    git.unreadable = True

    async def agent(data):
        status, _ = await post(wrapper, success=success, reason="blocked by environment")
        assert status == 200

    monkeypatch.setattr(wrapper, "execute_agent", agent)
    await wrapper._run_turn("lease-003", TURN)
    assert git.calls == []
    assert wrapper.broker.submit_output.await_args.args[1].status == (
        WorkerResultStatus.COMPLETED if success else WorkerResultStatus.BLOCKED
    )


@pytest.mark.parametrize(
    "missing,reason",
    [
        ("attempt_id", "ownership_missing"),
        ("branch", "branch_missing"),
    ],
)
async def test_missing_turn_identity_cannot_cross_barrier(owned_turn, monkeypatch, missing, reason):
    wrapper, git = owned_turn
    turn = {key: value for key, value in TURN.items() if key != missing}

    async def agent(data):
        status, body = await post(wrapper)
        assert status == 400
        assert body["publication"]["failure"] == reason
        assert not wrapper._result_event.is_set()
        assert wrapper._buffered_result is None
        assert git.calls == []
        assert (await post(wrapper, success=False, reason="identity refused"))[0] == 200

    monkeypatch.setattr(wrapper, "execute_agent", agent)
    await wrapper._run_turn("lease-003", turn)
    assert wrapper.broker.submit_output.await_args.args[1].status == WorkerResultStatus.BLOCKED


async def test_concurrent_terminal_submissions_accept_only_one():
    from worker_wrapper.http_server import ResultHttpServer

    entered, release, terminal = asyncio.Event(), asyncio.Event(), asyncio.Event()
    accepted = []

    async def buffer(result):
        entered.set()
        await release.wait()
        accepted.append(result)

    server = ResultHttpServer("worker", buffer, terminal)
    payload = json.dumps({"success": True, "commit": HEAD, "summary": "done"}).encode()
    first = asyncio.create_task(server._handle_result(payload))
    second = asyncio.create_task(server._handle_result(payload))
    await entered.wait()
    release.set()
    responses = await asyncio.gather(first, second)
    assert [status for status, _ in responses] == [200, 409]
    assert len(accepted) == 1
    assert terminal.is_set()
