"""A Claude turn's result carries only the stream's final result event, never tool output."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from conftest import FakePipe
import pytest
from structlog.testing import capture_logs
from worker_wrapper.config import WorkerWrapperConfig
from worker_wrapper.wrapper import WorkerWrapper

from shared.contracts.queues.worker_result import WorkerFailedResult

STREAM = Path(__file__).resolve().parents[1] / "fixtures" / "claude_code_2.1.278_stream.jsonl"
RESULT_LINE = STREAM.read_text(encoding="utf-8").strip().splitlines()[-1]
TOOL_OUTPUT = ("hello-from-bash", "File created successfully", "has been updated successfully")
MARKER = "tool-output-marker-7f3a"
TURN = {"request_id": "req-1", "prompt": "go", "branch": "story/story-1"}


def _wrapper(tmp_path, monkeypatch) -> WorkerWrapper:
    broker = MagicMock()
    broker.get_session = AsyncMock(return_value=None)
    broker.set_session = AsyncMock()
    broker.update_status = AsyncMock()
    broker.submit_output = AsyncMock()
    broker.compose = AsyncMock()
    config = WorkerWrapperConfig(
        broker_url="http://worker-broker:8001",
        broker_token="x" * 43,
        worker_id="dev-tail-1",
        agent_type="claude",
        transcript_dir=str(tmp_path),
    )
    wrapper = WorkerWrapper(config=config, broker_client=broker)
    monkeypatch.setattr(wrapper, "_git_auth_preflight", lambda: True)
    monkeypatch.setattr(wrapper, "_prepare_workspace", AsyncMock())
    monkeypatch.setattr(wrapper, "_check_workspace_ready", lambda: (True, "ready"))
    monkeypatch.setattr(wrapper, "_fix_venv_paths", lambda: None)
    monkeypatch.setattr(wrapper, "_install_compose_proxy", lambda: None)
    monkeypatch.setattr(wrapper, "_read_worker_report", lambda: None)
    monkeypatch.setattr(wrapper, "_archive_task", lambda *args: None)
    monkeypatch.setattr("worker_wrapper.http_server.ResultHttpServer.start", AsyncMock())
    monkeypatch.setattr("worker_wrapper.http_server.ResultHttpServer.stop", AsyncMock())
    return wrapper


def _process(stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = FakePipe(stdout)
    proc.stderr = FakePipe(stderr)
    proc.wait = AsyncMock(return_value=returncode)
    return proc


def _events(*events: dict) -> bytes:
    return b"".join(json.dumps(event).encode() + b"\n" for event in events)


def _tool_result(content: str) -> dict:
    block = {"tool_use_id": "toolu_1", "type": "tool_result", "content": content}
    return {"type": "user", "message": {"role": "user", "content": [block]}}


async def _turn(wrapper: WorkerWrapper, *processes: MagicMock) -> tuple[WorkerFailedResult, list]:
    with (
        capture_logs() as logs,
        patch(
            "worker_wrapper.wrapper.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=list(processes)),
        ),
    ):
        await wrapper.process_message("msg-1", TURN)
    [(_, result)] = [call.args for call in wrapper.broker.submit_output.await_args_list]
    return result, logs


@pytest.mark.asyncio
async def test_the_real_stream_leaves_only_its_result_event_on_the_result(tmp_path, monkeypatch):
    wrapper = _wrapper(tmp_path, monkeypatch)

    result, logs = await _turn(wrapper, _process(STREAM.read_bytes()))

    assert result.agent_stdout_tail == RESULT_LINE
    published = result.model_dump_json() + json.dumps(logs, default=str)
    for tool_output in TOOL_OUTPUT:
        assert tool_output not in published


@pytest.mark.asyncio
async def test_stderr_follows_the_result_event_on_the_tail(tmp_path, monkeypatch):
    wrapper = _wrapper(tmp_path, monkeypatch)

    result, _ = await _turn(wrapper, _process(STREAM.read_bytes(), b"cli warning"))

    assert result.agent_stdout_tail == f"{RESULT_LINE}\n--- stderr ---\ncli warning"


@pytest.mark.parametrize(("stderr", "tail"), [(b"", None), (b"cli warning", "cli warning")])
@pytest.mark.asyncio
async def test_a_stream_without_a_result_event_leaves_stderr_only(
    tmp_path, monkeypatch, stderr, tail
):
    wrapper = _wrapper(tmp_path, monkeypatch)

    result, _ = await _turn(wrapper, _process(_events(_tool_result(MARKER)), stderr))

    assert result.agent_stdout_tail == tail


@pytest.mark.asyncio
async def test_a_failed_exit_reports_at_most_the_result_event(tmp_path, monkeypatch):
    wrapper = _wrapper(tmp_path, monkeypatch)
    final = {"type": "result", "subtype": "error_during_execution", "is_error": True}
    stream = _events(_tool_result(MARKER), final)

    result, logs = await _turn(wrapper, _process(stream, returncode=1))

    assert result.error == f"Agent process failed with code 1: {json.dumps(final)}"
    [failure] = [entry for entry in logs if entry["event"] == "execution_failed"]
    assert failure["error"] == result.error
    assert MARKER not in result.model_dump_json() + json.dumps(logs, default=str)


@pytest.mark.asyncio
async def test_a_failed_exit_without_a_result_event_reports_no_output(tmp_path, monkeypatch):
    wrapper = _wrapper(tmp_path, monkeypatch)

    result, _ = await _turn(wrapper, _process(_events(_tool_result(MARKER)), returncode=1))

    assert result.error == "Agent process failed with code 1: no output"


@pytest.mark.asyncio
async def test_a_resume_without_a_result_event_or_stderr_leaves_no_tail(tmp_path, monkeypatch):
    wrapper = _wrapper(tmp_path, monkeypatch)
    wrapper.broker.get_session.side_effect = [None, "session-1"]

    result, _ = await _turn(
        wrapper, _process(STREAM.read_bytes()), _process(_events(_tool_result(MARKER)))
    )

    assert result.agent_stdout_tail is None
