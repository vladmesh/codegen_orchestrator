"""Data models for Agent Spawner."""

from dataclasses import dataclass


@dataclass
class MessageRequest:
    """Request to send message to agent."""

    user_id: str
    message: str
    chat_id: int | None = None
    message_id: int | None = None
    correlation_id: str | None = None
    session_id: str | None = None


@dataclass
class ExecutionResult:
    """Result from agent execution."""

    success: bool
    output: str
    session_id: str | None = None
    exit_code: int = 0
    error: str | None = None
