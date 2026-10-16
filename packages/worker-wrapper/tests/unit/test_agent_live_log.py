"""A Claude run's stream-json stdout becomes redacted, bounded live log lines while it runs."""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from conftest import FakePipe
import pytest
from structlog.testing import capture_logs
from worker_wrapper.agent_log import ClaudeLiveLog
from worker_wrapper.config import WorkerWrapperConfig
from worker_wrapper.wrapper import WorkerWrapper

from shared.contracts.dto.agent_log import AGENT_LOG_TEXT_MAX_CHARS, AgentLogLine

STREAM = Path(__file__).resolve().parents[1] / "fixtures" / "claude_code_2.1.278_stream.jsonl"
TURN = {"request_id": "req-1", "attempt_id": "attempt-1", "task_id": "task-1", "prompt": "go"}


def _wrapper(tmp_path) -> tuple[WorkerWrapper, MagicMock]:
    broker = MagicMock()
    broker.get_session = AsyncMock(return_value=None)
    broker.set_session = AsyncMock()
    config = WorkerWrapperConfig(
        broker_url="http://worker-broker:8001",
        broker_token="x" * 43,
        worker_id="dev-live-1",
        agent_type="claude",
        transcript_dir=str(tmp_path),
    )
    return WorkerWrapper(config=config, broker_client=broker), broker


def _process(stdout: bytes, exited: asyncio.Event | None = None) -> MagicMock:
    async def wait():
        if exited is not None:
            await exited.wait()
        return 0

    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = FakePipe(stdout, exited)
    proc.stderr = FakePipe(b"", exited)
    proc.wait = wait
    return proc


def _events(*events: dict) -> bytes:
    return b"".join(json.dumps(event).encode() + b"\n" for event in events)


def _bash(command: str, tool_id: str = "toolu_1") -> dict:
    block = {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}}
    return {"type": "assistant", "message": {"content": [block]}}


def _lines(logs: list[dict]) -> list[AgentLogLine]:
    return [
        AgentLogLine.model_validate(
            {key: value for key, value in entry.items() if key not in {"event", "log_level"}}
        )
        for entry in logs
        if entry["event"] == "agent_log_line"
    ]


def _fed(*chunks: bytes) -> list[AgentLogLine]:
    live_log = ClaudeLiveLog(attempt_id="attempt-1", task_id=None, environment={})
    with capture_logs() as logs:
        for chunk in chunks:
            live_log.feed(chunk)
        live_log.close()
    return _lines(logs)


@pytest.mark.asyncio
async def test_lines_are_logged_while_the_agent_is_still_running(tmp_path):
    wrapper, _ = _wrapper(tmp_path)
    exited = asyncio.Event()
    running = _events(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Reading"}]}},
        _bash("ls -la"),
    )

    with (
        capture_logs() as logs,
        patch(
            "worker_wrapper.wrapper.asyncio.create_subprocess_exec",
            AsyncMock(return_value=_process(running, exited)),
        ),
    ):
        turn = asyncio.create_task(wrapper.execute_agent(TURN))
        for _ in range(50):
            if len(_lines(logs)) == 2:
                break
            await asyncio.sleep(0)
        seen_before_exit = [(line.kind, line.text) for line in _lines(logs)]
        assert not turn.done()
        exited.set()
        await asyncio.wait_for(turn, timeout=1)

    assert seen_before_exit == [("info", "Reading"), ("cmd", "ls -la")]


@pytest.mark.asyncio
async def test_the_stream_maps_to_typed_lines_without_tool_output(tmp_path):
    wrapper, broker = _wrapper(tmp_path)

    with (
        capture_logs() as logs,
        patch(
            "worker_wrapper.wrapper.asyncio.create_subprocess_exec",
            AsyncMock(return_value=_process(STREAM.read_bytes())),
        ),
    ):
        await wrapper.execute_agent(TURN)

    lines = _lines(logs)
    assert [(line.seq, line.kind, line.text) for line in lines] == [
        (0, "info", "I'll read the task, create hello.txt and report the result."),
        (1, "ok", "Read"),
        (2, "file", "hello.txt"),
        (3, "ok", "Write"),
        (
            4,
            "cmd",
            "curl -s -X POST http://localhost:9090/result -H 'Content-Type: application/json' "
            '-d \'{"success": true, "summary": "created hello.txt"}\'',
        ),
        (5, "ok", "Bash"),
        (6, "info", 'Done — created hello.txt with "hello" and reported success.'),
        (7, "step", "turn ended: success"),
    ]
    assert {(line.attempt_id, line.task_id) for line in lines} == {("attempt-1", "task-1")}
    emitted = " ".join(line.text for line in lines)
    for tool_output in ("Create hello.txt containing hello", "File created", "accepted"):
        assert tool_output not in emitted
    broker.set_session.assert_awaited_once_with("00000000-0000-4000-8000-000000000001")


@pytest.mark.asyncio
async def test_a_secret_from_the_agent_environment_is_redacted_in_a_command(tmp_path):
    wrapper, _ = _wrapper(tmp_path)
    secret = "sk-ant-live-log-secret-value"  # noqa: S105

    with (
        capture_logs() as logs,
        patch.dict(os.environ, {"ANTHROPIC_API_KEY": secret}),
        patch(
            "worker_wrapper.wrapper.asyncio.create_subprocess_exec",
            AsyncMock(return_value=_process(_events(_bash(f"echo {secret} > key.txt")))),
        ) as spawn,
    ):
        await wrapper.execute_agent(TURN)

    assert spawn.await_args.kwargs["env"]["ANTHROPIC_API_KEY"] == secret
    assert [line.text for line in _lines(logs)] == ["echo [redacted] > key.txt"]


@pytest.mark.asyncio
async def test_seq_continues_across_the_auto_resume(tmp_path):
    wrapper, broker = _wrapper(tmp_path)
    wrapper._result_event = asyncio.Event()
    wrapper._git_auth_preflight = MagicMock(return_value=True)
    first = _process(_events(_bash("ls")))
    resumed = _process(_events({"type": "result", "subtype": "success", "is_error": False}))

    with (
        capture_logs() as logs,
        patch(
            "worker_wrapper.wrapper.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=[first, resumed]),
        ),
    ):
        await wrapper.execute_agent(TURN)
        broker.get_session.return_value = "session-1"
        assert await wrapper._attempt_auto_resume(TURN) is True

    assert [(line.seq, line.kind) for line in _lines(logs)] == [(0, "cmd"), (1, "step")]


@pytest.mark.asyncio
async def test_a_turn_without_an_attempt_logs_no_line(tmp_path):
    wrapper, _ = _wrapper(tmp_path)

    with (
        capture_logs() as logs,
        patch(
            "worker_wrapper.wrapper.asyncio.create_subprocess_exec",
            AsyncMock(return_value=_process(STREAM.read_bytes())),
        ),
    ):
        await wrapper.execute_agent({"request_id": "qa-1", "prompt": "explore"})

    assert _lines(logs) == []


def test_an_event_split_across_reads_is_logged_once_it_is_complete():
    event = _events(_bash("pytest -q"))
    live_log = ClaudeLiveLog(attempt_id="attempt-1", task_id=None, environment={})

    with capture_logs() as logs:
        live_log.feed(event[:20])
        before = _lines(logs)
        live_log.feed(event[20:])

    assert before == []
    assert [(line.kind, line.text) for line in _lines(logs)] == [("cmd", "pytest -q")]


def test_tool_errors_failed_turns_and_file_tools_map_to_their_kinds():
    def tool(name: str, field: str, path: str) -> dict:
        block = {"type": "tool_use", "id": name, "name": name, "input": {field: path}}
        return {"type": "assistant", "message": {"content": [block]}}

    failed = {"type": "tool_result", "tool_use_id": "Edit", "content": "boom", "is_error": True}
    lines = _fed(
        _events(
            tool("Edit", "file_path", "a.py"),
            tool("MultiEdit", "file_path", "b.py"),
            tool("NotebookEdit", "notebook_path", "c.ipynb"),
            tool("Grep", "pattern", "TODO"),
            {"type": "user", "message": {"content": [failed]}},
            {"type": "user", "message": {"content": "a plain prompt"}},
            {"type": "result", "subtype": "error_max_turns", "is_error": True},
        )
    )

    assert [(line.kind, line.text) for line in lines] == [
        ("file", "a.py"),
        ("file", "b.py"),
        ("file", "c.ipynb"),
        ("err", "Edit"),
        ("step", "turn ended: error (error_max_turns)"),
    ]


def test_text_is_bounded_after_redaction():
    lines = _fed(_events(_bash("x" * (AGENT_LOG_TEXT_MAX_CHARS * 3))))

    assert len(lines[0].text) == AGENT_LOG_TEXT_MAX_CHARS
    assert lines[0].text.endswith("…")


def test_output_that_is_not_a_stream_event_logs_nothing():
    assert _fed(b"Done.\n", b"[1, 2]\n", b'{"type": "system", "subtype": "init"}') == []
