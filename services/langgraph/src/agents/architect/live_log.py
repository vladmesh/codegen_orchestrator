"""The architect graph's planning run as live log lines."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage, ToolMessage

from shared.contracts.dto.agent_log import AgentLogKind, AgentLogWriter


class ArchitectLiveLog(AsyncCallbackHandler):
    """Write a claimed planning attempt's model and tool lines as the graph's nodes finish.

    Tool results reach a line by tool name only, never by content. The attempt's terminal
    step is the consumer's, written once its outcome is known.
    """

    raise_error = True

    def __init__(self, writer: AgentLogWriter):
        self._writer = writer
        self._root: UUID | None = None
        self._tool_names: dict[UUID, str] = {}

    async def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        if parent_run_id is None:
            self._root = run_id

    async def on_chain_end(
        self,
        outputs: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        if parent_run_id == self._root and isinstance(outputs, dict):
            for message in outputs.get("messages", []):
                if isinstance(message, AIMessage):
                    _write_model_turn(self._writer, message)

    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        self._tool_names[run_id] = serialized["name"]

    async def on_tool_end(
        self,
        output: ToolMessage,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        kind = AgentLogKind.ERR if _tool_failed(output) else AgentLogKind.OK
        self._writer.write(kind, self._tool_names.pop(run_id))

    async def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        self._writer.write(AgentLogKind.ERR, self._tool_names.pop(run_id))


def _write_model_turn(writer: AgentLogWriter, message: AIMessage) -> None:
    if message.text.strip():
        writer.write(AgentLogKind.INFO, message.text)
    for call in message.tool_calls:
        writer.write(
            AgentLogKind.CMD, f"{call['name']} {json.dumps(call['args'], ensure_ascii=False)}"
        )


def _tool_failed(output: ToolMessage) -> bool:
    """Whether a tool raised a handled error or answered with an ``error`` object."""
    if output.status == "error":
        return True
    try:
        answer = json.loads(output.content) if isinstance(output.content, str) else None
    except ValueError:
        return False
    return isinstance(answer, dict) and "error" in answer
