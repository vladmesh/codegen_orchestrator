"""API Key schemas."""

from pydantic import BaseModel, ConfigDict


class APIKeyBase(BaseModel):
    """Base API key schema."""

    service: str
    type: str = "system"
    project_id: str | None = None


class APIKeyCreate(APIKeyBase):
    """Schema for creating an API key."""

    value: dict | str  # Accept dict (for JSON) or str


class APIKeyRead(APIKeyBase):
    """Schema for reading an API key."""

    id: int
    # key_enc is internal, not exposed directly in read model usually?
    # but for internal usage we might need it. Let's expose it for now
    # as we don't have a separate "decryption" endpoint yet.
    # Actually, better to expose nothing sensitive.

    model_config = ConfigDict(from_attributes=True)
