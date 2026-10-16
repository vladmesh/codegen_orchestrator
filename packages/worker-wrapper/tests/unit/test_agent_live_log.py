"""An agent run's JSON event stdout becomes redacted, bounded live log lines while it runs."""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from conftest import FakePipe
import pytest
from structlog.testing import capture_logs
from worker_wrapper.agent_log import ClaudeLiveLog, CodexLiveLog
from worker_wrapper.config import WorkerWrapperConfig
from worker_wrapper.runners.codex import CodexRunner
from worker_wrapper.wrapper import WorkerWrapper

from shared.contracts.dto.agent_log import AGENT_LOG_TEXT_MAX_CHARS, AgentLogLine

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
STREAM = FIXTURES / "claude_code_2.1.278_stream.jsonl"
CODEX_STREAM = FIXTURES / "codex_0.144.6_exec_stream.jsonl"
TURN = {"request_id": "req-1", "attempt_id": "attempt-1", "task_id": "task-1", "prompt": "go"}
QA_TURN = {"request_id": "qa-1", "prompt": "explore"}


def _wrapper(tmp_path, agent_type: str = "claude", **config) -> tuple[WorkerWrapper, MagicMock]:
    broker = MagicMock()
    broker.get_session = AsyncMock(return_value=None)
    broker.set_session = AsyncMock()
    config = WorkerWrapperConfig(
        broker_url="http://worker-broker:8001",
        broker_token="x" * 43,
        worker_id="dev-live-1",
        agent_type=agent_type,
        # Codex in api_key mode runs without the shared host-profile lock.
        auth_mode="api_key" if agent_type == "codex" else "host_session",
        transcript_dir=str(tmp_path),
        **config,
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


def _fed(*chunks: bytes, live_log_type=ClaudeLiveLog, environment=None) -> list[AgentLogLine]:
    live_log = live_log_type(attempt_id="attempt-1", task_id=None, environment=environment or {})
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
        (0, "cmd", "echo hello-from-bash"),
        (1, "ok", "Bash"),
        (2, "file", "/workspace/notes.txt"),
        (3, "ok", "Write"),
        (4, "file", "/workspace/notes.txt"),
        (5, "ok", "Edit"),
        (
            6,
            "info",
            'Done: echoed the string, created notes.txt with "first", then edited it to "second".',
        ),
        (7, "step", "turn ended: success"),
    ]
    assert {(line.attempt_id, line.task_id) for line in lines} == {("attempt-1", "task-1")}
    emitted = " ".join(line.text for line in lines)
    for tool_output in ("File created successfully", "has been updated successfully", "-first"):
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
@pytest.mark.parametrize(
    ("agent_type", "stream", "worker_type"),
    [
        ("claude", STREAM, "developer"),
        ("codex", CODEX_STREAM, "developer"),
        ("claude", STREAM, "qa"),
        ("codex", CODEX_STREAM, "qa"),
    ],
)
async def test_a_turn_without_an_attempt_or_qa_run_logs_no_line(
    tmp_path, agent_type, stream, worker_type
):
    wrapper, _ = _wrapper(tmp_path, agent_type, worker_type=worker_type)

    with (
        capture_logs() as logs,
        patch(
            "worker_wrapper.wrapper.asyncio.create_subprocess_exec",
            AsyncMock(return_value=_process(stream.read_bytes())),
        ),
    ):
        await wrapper.execute_agent(QA_TURN)

    assert _lines(logs) == []


@pytest.mark.asyncio
async def test_the_codex_stream_maps_to_typed_lines_without_command_output(tmp_path):
    wrapper, _ = _wrapper(tmp_path, "codex")

    with (
        capture_logs() as logs,
        patch(
            "worker_wrapper.wrapper.asyncio.create_subprocess_exec",
            AsyncMock(return_value=_process(CODEX_STREAM.read_bytes())),
        ) as spawn,
    ):
        await wrapper.execute_agent(TURN)

    assert list(spawn.await_args.args) == CodexRunner().build_command(
        prompt=wrapper._resolve_prompt(TURN)
    )
    lines = _lines(logs)
    assert [(line.seq, line.kind, line.text) for line in lines] == [
        (0, "cmd", "/bin/bash -lc 'cat TASK.md'"),
        (1, "ok", "exit code 0"),
        (2, "cmd", "/bin/bash -lc 'echo hello-from-bash'"),
        (3, "ok", "exit code 0"),
        (4, "file", "/workspace/notes.txt"),
        (5, "cmd", "/bin/bash -lc 'cat notes.txt missing.txt'"),
        (6, "err", "exit code 1"),
        (7, "info", 'Done: echoed hello-from-bash and created notes.txt with "first".'),
        (8, "step", "turn ended: success"),
    ]
    assert {(line.attempt_id, line.task_id) for line in lines} == {("attempt-1", "task-1")}
    emitted = "\n".join(line.text for line in lines)
    for aggregated_output in ("# Task", "containing first", "No such file or directory"):
        assert aggregated_output not in emitted
    assert wrapper._agent_stdout_tail is None
    assert wrapper._effort_metrics == {}


@pytest.mark.asyncio
async def test_a_secret_from_the_agent_environment_is_redacted_in_a_codex_command(tmp_path):
    wrapper, _ = _wrapper(tmp_path, "codex")
    secret = "sk-proj-codex-live-log-secret-value"  # noqa: S105
    command = f"/bin/bash -lc 'echo {secret} > key.txt'"
    started = {
        "type": "item.started",
        "item": {"id": "item_1", "type": "command_execution", "command": command},
    }

    with (
        capture_logs() as logs,
        patch.dict(os.environ, {"CODEX_API_KEY": secret}),
        patch(
            "worker_wrapper.wrapper.asyncio.create_subprocess_exec",
            AsyncMock(return_value=_process(_events(started))),
        ) as spawn,
    ):
        await wrapper.execute_agent(TURN)

    assert spawn.await_args.kwargs["env"]["CODEX_API_KEY"] == secret
    assert [(line.kind, line.text) for line in _lines(logs)] == [
        ("cmd", "/bin/bash -lc 'echo [redacted] > key.txt'")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("agent_type", "stream"), [("codex", CODEX_STREAM), ("claude", STREAM)])
async def test_a_qa_turn_logs_its_lines_under_the_qa_run(tmp_path, agent_type, stream):
    wrapper, _ = _wrapper(tmp_path, agent_type, worker_type="qa", qa_run_id="qa-run-1")

    with (
        capture_logs() as logs,
        patch(
            "worker_wrapper.wrapper.asyncio.create_subprocess_exec",
            AsyncMock(return_value=_process(stream.read_bytes())),
        ),
    ):
        await wrapper.execute_agent(QA_TURN)

    lines = _lines(logs)
    assert lines[-1].text == "turn ended: success"
    assert {(line.attempt_id, line.task_id) for line in lines} == {("qa-run-1", None)}


def test_codex_failures_end_in_a_redacted_bounded_step():
    secret = "sk-proj-codex-turn-failure-secret"  # noqa: S105
    lines = _fed(
        _events(
            {"type": "error", "message": f"stream disconnected; key {secret}"},
            {"type": "turn.failed", "error": {"message": "x" * (AGENT_LOG_TEXT_MAX_CHARS * 2)}},
        ),
        live_log_type=CodexLiveLog,
        environment={"OPENAI_API_KEY": secret},
    )

    assert [line.kind for line in lines] == ["step", "step"]
    assert lines[0].text == "error: stream disconnected; key [redacted]"
    assert lines[1].text.startswith("turn ended: error (xxx")
    assert len(lines[1].text) == AGENT_LOG_TEXT_MAX_CHARS
    assert lines[1].text.endswith("…")


def test_codex_reasoning_usage_and_unknown_events_log_nothing():
    lines = _fed(
        _events(
            {"type": "thread.started", "thread_id": "t-1"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "i0", "type": "reasoning", "text": "hm"}},
            {"type": "item.updated", "item": {"id": "i1", "type": "todo_list", "items": []}},
            {"type": "item.completed", "item": {"id": "i2", "type": "error", "message": "warn"}},
            {"type": "item.started", "item": {"id": "i3", "type": "mcp_tool_call"}},
            {
                "type": "item.completed",
                "item": {"id": "i4", "type": "file_change", "changes": [], "status": "completed"},
            },
            {"type": "thread.usage", "usage": {"input_tokens": 1}},
        ),
        live_log_type=CodexLiveLog,
    )

    assert lines == []


def test_a_codex_file_change_logs_each_applied_path():
    def change(status: str, *paths: str) -> dict:
        changes = [{"path": path, "kind": "update"} for path in paths]
        item = {"id": "i", "type": "file_change", "changes": changes, "status": status}
        return {"type": "item.completed", "item": item}

    lines = _fed(
        _events(change("completed", "a.py", "b.py"), change("failed", "c.py")),
        live_log_type=CodexLiveLog,
    )

    assert [(line.kind, line.text) for line in lines] == [("file", "a.py"), ("file", "b.py")]


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
