from pydantic import BaseModel, ConfigDict


class UserDTO(BaseModel):
    """User response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    telegram_id: int

    is_admin: bool = False
