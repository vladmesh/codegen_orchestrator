from typing import Literal

from shared.contracts.dto.base import TimestampedDTO


class AgentConfigDTO(TimestampedDTO):
    """Agent configuration response."""

    id: int
    name: str
    type: Literal["claude", "factory"]
    model: str
    system_prompt: str
    is_active: bool = True
