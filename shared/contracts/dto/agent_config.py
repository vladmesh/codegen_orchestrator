from typing import Literal

from pydantic import BaseModel, ConfigDict


class AgentConfigDTO(BaseModel):
    """Agent configuration response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    type: Literal["claude", "factory"]
    model: str
    system_prompt: str
    is_active: bool = True
