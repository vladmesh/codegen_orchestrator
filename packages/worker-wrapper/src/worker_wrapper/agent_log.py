"""A coding agent's JSON event stdout as the run's live log lines."""

from __future__ import annotations

from collections.abc import Iterator
import json
from typing import Any

from shared.contracts.dto.agent_log import AgentLogKind, AgentLogWriter
from shared.diagnostics import environment_secrets

#: File-writing Claude tools and the input field naming the file each one writes.
FILE_WRITING_TOOLS = {
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
}

TURN_SUCCESS_TEXT = "turn ended: success"


class AgentLiveLog:
    """Write an ``agent_log_line`` event for each recognised stream event as its bytes arrive.

    Every text is redacted with ``environment`` and then bounded; tool output never
    reaches a line. One instance serves every agent process of an attempt.
    """

    def __init__(self, *, attempt_id: str, task_id: str | None, environment: dict[str, str]):
        self._writer = AgentLogWriter(
            attempt_id=attempt_id, task_id=task_id, secrets=environment_secrets(environment)
        )
        self._pending = bytearray()

    def feed(self, chunk: bytes) -> None:
        """Take the next stdout bytes and log every event line they complete."""
        self._pending += chunk
        if b"\n" not in chunk:
            return
        *lines, rest = bytes(self._pending).split(b"\n")
        self._pending = bytearray(rest)
        for line in lines:
            self._log_event_line(line)

    def close(self) -> None:
        """Log the last event of a stdout that did not end with a newline."""
        rest = bytes(self._pending)
        self._pending.clear()
        self._log_event_line(rest)

    def _log_event_line(self, line: bytes) -> None:
        if not line.strip():
            return
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        for kind, text in self._entries(event):
            self._writer.write(kind, text)

    def _entries(self, event: dict[str, Any]) -> Iterator[tuple[AgentLogKind, str]]:
        raise NotImplementedError


class ClaudeLiveLog(AgentLiveLog):
    """The Claude CLI's ``stream-json`` events as live log lines."""

    def __init__(self, *, attempt_id: str, task_id: str | None, environment: dict[str, str]):
        super().__init__(attempt_id=attempt_id, task_id=task_id, environment=environment)
        self._tool_names: dict[str, str] = {}

    def _entries(self, event: dict[str, Any]) -> Iterator[tuple[AgentLogKind, str]]:
        event_type = event.get("type")
        if event_type == "assistant":
            for block in _content_blocks(event):
                yield from self._assistant_entries(block)
        elif event_type == "user":
            for block in _content_blocks(event):
                if block.get("type") == "tool_result":
                    kind = AgentLogKind.ERR if block.get("is_error") is True else AgentLogKind.OK
                    yield kind, self._tool_names.get(str(block.get("tool_use_id")), "tool")
        elif event_type == "result":
            yield AgentLogKind.STEP, _claude_turn_end(event)

    def _assistant_entries(self, block: dict[str, Any]) -> Iterator[tuple[AgentLogKind, str]]:
        if block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                yield AgentLogKind.INFO, text
            return
        name, tool_input = block.get("name"), block.get("input")
        if block.get("type") != "tool_use" or not isinstance(name, str):
            return
        if isinstance(block.get("id"), str):
            self._tool_names[block["id"]] = name
        if not isinstance(tool_input, dict):
            return
        if name == "Bash" and isinstance(tool_input.get("command"), str):
            yield AgentLogKind.CMD, tool_input["command"]
        elif name in FILE_WRITING_TOOLS:
            path = tool_input.get(FILE_WRITING_TOOLS[name])
            if isinstance(path, str):
                yield AgentLogKind.FILE, path


class CodexLiveLog(AgentLiveLog):
    """``codex exec --json`` events as live log lines; command output never reaches one."""

    def _entries(self, event: dict[str, Any]) -> Iterator[tuple[AgentLogKind, str]]:
        event_type = event.get("type")
        item = event.get("item")
        if event_type == "item.started" and isinstance(item, dict):
            command = item.get("command")
            if item.get("type") == "command_execution" and isinstance(command, str):
                yield AgentLogKind.CMD, command
        elif event_type == "item.completed" and isinstance(item, dict):
            yield from _codex_completed_item(item)
        elif event_type == "turn.completed":
            yield AgentLogKind.STEP, TURN_SUCCESS_TEXT
        elif event_type == "turn.failed":
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            yield AgentLogKind.STEP, f"turn ended: error ({message})"
        elif event_type == "error":
            yield AgentLogKind.STEP, f"error: {event.get('message')}"


def _codex_completed_item(item: dict[str, Any]) -> Iterator[tuple[AgentLogKind, str]]:
    item_type = item.get("type")
    if item_type == "agent_message":
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            yield AgentLogKind.INFO, text
    elif item_type == "command_execution":
        exit_code = item.get("exit_code")
        kind = AgentLogKind.OK if exit_code == 0 else AgentLogKind.ERR
        yield kind, f"exit code {exit_code if isinstance(exit_code, int) else 'unknown'}"
    elif item_type == "file_change" and item.get("status") == "completed":
        for change in item.get("changes") or []:
            if isinstance(change, dict) and isinstance(change.get("path"), str):
                yield AgentLogKind.FILE, change["path"]


def _content_blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _claude_turn_end(result: dict[str, Any]) -> str:
    subtype = result.get("subtype")
    if result.get("is_error") is False and subtype == "success":
        return TURN_SUCCESS_TEXT
    return f"turn ended: error ({subtype})" if isinstance(subtype, str) else "turn ended: error"
