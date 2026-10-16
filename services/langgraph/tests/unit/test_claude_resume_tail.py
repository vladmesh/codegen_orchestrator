"""A Claude turn recovered by the auto-resume carries the resumed process's result alone."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from worker_wrapper.config import WorkerWrapperConfig
from worker_wrapper.wrapper import WorkerWrapper

from src.clients.worker_spawner import spawn_result_from_output
from src.nodes.developer import DeveloperNode

STREAM = (
    Path(__file__).resolve().parents[4]
    / "packages"
    / "worker-wrapper"
    / "tests"
    / "fixtures"
    / "claude_code_2.1.278_stream.jsonl"
)
FIRST_RUN_TOOL_OUTPUT = ("hello-from-bash", "File created successfully")
RESUME_MARKER = "resume-tool-output-marker-41c2"
RESUMED = {"type": "result", "subtype": "success", "modelUsage": {"claude-opus-5": {}}}


class _Pipe:
    def __init__(self, data: bytes):
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        size = len(self._data) if n < 0 else n
        chunk, self._data = self._data[:size], self._data[size:]
        return chunk


def _process(stdout: bytes, stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = _Pipe(stdout)
    proc.stderr = _Pipe(stderr)
    proc.wait = AsyncMock(return_value=0)
    return proc


def _wrapper(tmp_path, monkeypatch) -> WorkerWrapper:
    broker = MagicMock()
    broker.get_session = AsyncMock(side_effect=[None, "session-1"])
    broker.set_session = AsyncMock()
    broker.update_status = AsyncMock()
    broker.submit_output = AsyncMock()
    broker.compose = AsyncMock()
    config = WorkerWrapperConfig(
        broker_url="http://worker-broker:8001",
        broker_token="x" * 43,
        worker_id="dev-resume-1",
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


@pytest.mark.parametrize(
    ("stderr", "stderr_suffix"),
    [(b"", ""), (b"resume warning", "\n--- stderr ---\nresume warning")],
)
async def test_the_auto_resume_tail_is_the_resumed_result_and_names_its_model(
    tmp_path, monkeypatch, stderr, stderr_suffix
):
    wrapper = _wrapper(tmp_path, monkeypatch)
    tool_result = {"type": "tool_result", "tool_use_id": "toolu_1", "content": RESUME_MARKER}
    resume_stream = (
        json.dumps({"type": "user", "message": {"content": [tool_result]}})
        + "\n"
        + json.dumps(RESUMED)
        + "\n"
    ).encode()

    with patch(
        "worker_wrapper.wrapper.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=[_process(STREAM.read_bytes()), _process(resume_stream, stderr)]),
    ):
        await wrapper.process_message("msg-1", {"request_id": "req-1", "prompt": "go"})

    [(_, published)] = [call.args for call in wrapper.broker.submit_output.await_args_list]
    assert published.agent_stdout_tail == json.dumps(RESUMED) + stderr_suffix
    payload = published.model_dump_json()
    for tool_output in (*FIRST_RUN_TOOL_OUTPUT, RESUME_MARKER):
        assert tool_output not in payload
    spawned = spawn_result_from_output(
        published.model_dump(mode="json"), request_id="req-1", worker_id="dev-resume-1"
    )
    assert DeveloperNode._reported_model(spawned.logs_tail) == "claude-opus-5"
