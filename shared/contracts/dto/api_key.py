from pydantic import BaseModel, ConfigDict


class APIKeyDTO(BaseModel):
    """API Key response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    service: str
    key_enc: str
    created_at: str | None = None
