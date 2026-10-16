"""The Claude CLI's ``stream-json`` events as the attempt's live log lines."""

from __future__ import annotations

from collections.abc import Iterator
import json
from typing import Any

import structlog

from shared.contracts.dto.agent_log import (
    AGENT_LOG_LINE_EVENT,
    AGENT_LOG_TEXT_MAX_CHARS,
    AgentLogKind,
    AgentLogLine,
)

from .observability import redact_transcript

logger = structlog.get_logger(__name__)

#: File-writing Claude tools and the input field naming the file each one writes.
FILE_WRITING_TOOLS = {
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
}


class ClaudeLiveLog:
    """Write an ``agent_log_line`` event for each recognised stream event as its bytes arrive.

    Every text is redacted with ``environment`` and then bounded; tool output never
    reaches a line. One instance serves every agent process of an attempt.
    """

    def __init__(self, *, attempt_id: str, task_id: str | None, environment: dict[str, str]):
        self._attempt_id = attempt_id
        self._task_id = task_id
        self._environment = environment
        self._seq = 0
        self._pending = bytearray()
        self._tool_names: dict[str, str] = {}

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
            self._write(kind, text)

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
            yield AgentLogKind.STEP, _turn_end(event)

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

    def _write(self, kind: AgentLogKind, text: str) -> None:
        # Redaction comes first: a bound cut through a secret would leave a prefix
        # the redactor no longer recognises.
        text = redact_transcript(text, self._environment)
        if len(text) > AGENT_LOG_TEXT_MAX_CHARS:
            text = text[: AGENT_LOG_TEXT_MAX_CHARS - 1] + "…"
        line = AgentLogLine(
            attempt_id=self._attempt_id,
            task_id=self._task_id,
            seq=self._seq,
            kind=kind,
            text=text,
        )
        self._seq += 1
        logger.info(AGENT_LOG_LINE_EVENT, **line.model_dump(mode="json"))


def _content_blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _turn_end(result: dict[str, Any]) -> str:
    subtype = result.get("subtype")
    if result.get("is_error") is False and subtype == "success":
        return "turn ended: success"
    return f"turn ended: error ({subtype})" if isinstance(subtype, str) else "turn ended: error"
