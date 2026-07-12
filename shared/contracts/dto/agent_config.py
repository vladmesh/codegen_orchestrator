from shared.contracts.dto.base import TimestampedDTO
from shared.contracts.vocab import AgentType


class AgentConfigDTO(TimestampedDTO):
    """Agent configuration response."""

    id: int
    name: str
    type: AgentType
    model: str
    system_prompt: str
    is_active: bool = True
