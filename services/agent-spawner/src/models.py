"""Data models for Agent Spawner."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ContainerStatus(str, Enum):
    """Container lifecycle states."""

    CREATING = "creating"
    RUNNING = "running"
    PAUSED = "paused"
    DESTROYED = "destroyed"


@dataclass
class AgentSession:
    """Represents a user's agent session."""

    user_id: str
    container_id: str | None = None
    claude_session_id: str | None = None
    status: ContainerStatus = ContainerStatus.CREATING
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_activity_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        """Convert to dictionary for Redis storage."""
        return {
            "user_id": self.user_id,
            "container_id": self.container_id or "",
            "claude_session_id": self.claude_session_id or "",
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "last_activity_at": self.last_activity_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AgentSession":
        """Create from dictionary."""
        return cls(
            user_id=data["user_id"],
            container_id=data.get("container_id") or None,
            claude_session_id=data.get("claude_session_id") or None,
            status=ContainerStatus(data.get("status", "creating")),
            created_at=datetime.fromisoformat(data["created_at"]),
            last_activity_at=datetime.fromisoformat(data["last_activity_at"]),
        )


@dataclass
class MessageRequest:
    """Request to send message to agent."""

    user_id: str
    message: str
    session_id: str | None = None


@dataclass
class ExecutionResult:
    """Result from agent execution."""

    success: bool
    output: str
    session_id: str | None = None
    exit_code: int = 0
    error: str | None = None
