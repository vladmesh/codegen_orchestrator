"""CI-only: actual Git, native producer, HTTP intake and non-force publication."""

import asyncio
from http.client import HTTPConnection
import json
import subprocess
from unittest.mock import AsyncMock

import pytest
from worker_wrapper import report_completed
from worker_wrapper.config import WorkerWrapperConfig
from worker_wrapper.wrapper import WorkerWrapper

pytestmark = pytest.mark.subprocess

BRANCH = "story/native-report"


def git(path, *args):
    return subprocess.run(  # noqa: S603
        ["/usr/bin/git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()


def commit(path, text):
    (path / "app.py").write_text(text)
    git(path, "add", "app.py")
    git(path, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", text)
    return git(path, "rev-parse", "HEAD")


def post(port, payload):
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(
            "POST", "/result", json.dumps(payload), {"Content-Type": "application/json"}
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def prepared_repo(tmp_path):
    repo, origin = tmp_path / "checkout", tmp_path / "origin.git"
    repo.mkdir()
    git(tmp_path, "init", "--bare", str(origin))
    git(repo, "init", "-b", BRANCH)
    baseline = commit(repo, "initial")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "origin", BRANCH)
    sha = commit(repo, "feature")
    return repo, origin, baseline, sha


@pytest.mark.parametrize("after_accept", ["publish", "head_changed", "push_refused"])
async def test_native_same_turn_correction_and_final_publication(
    tmp_path, monkeypatch, after_accept
):
    repo, origin, baseline, sha = prepared_repo(tmp_path)
    monkeypatch.setattr("worker_wrapper.wrapper.WORKSPACE_DIR", str(repo))
    broker = AsyncMock()
    wrapper = WorkerWrapper(
        WorkerWrapperConfig(
            broker_url="http://broker.test",
            broker_token="x" * 43,
            worker_id="native-worker",
            agent_type="codex",
            http_server_port=0,
        ),
        broker_client=broker,
    )
    monkeypatch.setattr(wrapper, "_git_auth_preflight", lambda: True)
    monkeypatch.setattr(wrapper, "_prepare_workspace", AsyncMock())
    monkeypatch.setattr(wrapper, "_check_workspace_ready", lambda: (True, "ready"))
    monkeypatch.setattr(wrapper, "_fix_venv_paths", lambda: None)
    monkeypatch.setattr(wrapper, "_install_compose_proxy", lambda: None)
    monkeypatch.setattr(wrapper, "_read_worker_report", lambda: None)
    monkeypatch.setattr(wrapper, "_archive_task", lambda *args: None)
    native_payload = report_completed.completed_payload
    monkeypatch.setattr(
        report_completed, "completed_payload", lambda summary: native_payload(summary, repo)
    )
    summary = 'actual native commit "quoted"\nПривет'
    monkeypatch.setattr("sys.argv", ["worker-report-completed", "--summary", summary])
    agent_finished = []

    async def agent(data):
        port = wrapper._http_server.port
        for claim, reason in [("0" * 40, "object_missing"), (baseline, "head_changed")]:
            status, evidence = await asyncio.to_thread(
                post,
                port,
                {
                    "success": True,
                    "commit": claim,
                    "summary": summary,
                },
            )
            assert status == 400
            assert evidence["publication"]["failure"] == reason
            assert not wrapper._result_event.is_set()
            assert wrapper._buffered_result is None
            broker.submit_output.assert_not_awaited()
            assert git(origin, "rev-parse", f"refs/heads/{BRANCH}") == baseline
        git(repo, "checkout", "-b", "story/foreign")
        status, evidence = await asyncio.to_thread(
            post,
            port,
            {
                "success": True,
                "commit": sha,
                "summary": summary,
            },
        )
        assert status == 400
        assert evidence["publication"]["failure"] == "wrong_branch"
        assert not wrapper._result_event.is_set()
        git(repo, "checkout", BRANCH)
        monkeypatch.setattr(
            report_completed,
            "HTTPConnection",
            lambda *args, **kwargs: HTTPConnection(
                "127.0.0.1",
                port,
                timeout=10,
            ),
        )
        with pytest.raises(SystemExit) as exc:
            await asyncio.to_thread(report_completed.main)
        assert exc.value.code == 0
        assert wrapper._buffered_result.commit_sha == sha
        assert wrapper._result_event.is_set()
        status, _ = await asyncio.to_thread(
            post,
            port,
            {
                "success": True,
                "commit": sha,
                "summary": "duplicate",
            },
        )
        assert status == 409
        if after_accept == "head_changed":
            commit(repo, "changed after acceptance")
        elif after_accept == "push_refused":
            competitor = tmp_path / "competitor"
            git(tmp_path, "clone", "-b", BRANCH, str(origin), str(competitor))
            commit(competitor, "competing change")
            git(competitor, "push", "origin", BRANCH)
        agent_finished.append(True)

    monkeypatch.setattr(wrapper, "execute_agent", agent)
    await wrapper._run_turn(
        "lease-native",
        {
            "branch": BRANCH,
            "attempt_id": "attempt-native",
            "prompt": "complete feature",
        },
    )
    broker.submit_output.assert_awaited_once()
    assert agent_finished == [True]
    result = broker.submit_output.await_args.args[1]
    if after_accept == "publish":
        assert result.status == "completed"
        assert result.commit_sha == sha
        assert git(origin, "rev-parse", f"refs/heads/{BRANCH}") == sha
    else:
        assert result.status == "failed"
        assert result.publication.failure == after_accept
        assert result.publication.commit_sha == sha
        assert result.publication.attempt_id == "attempt-native"
        assert git(origin, "rev-parse", f"refs/heads/{BRANCH}") != sha
        assert result.worker_report == summary
