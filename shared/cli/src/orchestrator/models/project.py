"""Pydantic models for project-related CLI commands."""

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    """Model for project creation validation.

    The CLI will auto-generate the project ID (UUID) - agents only provide the name.
    """

    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Project name",
        examples=["my-telegram-bot", "hello-world-api"],
    )


class SecretSet(BaseModel):
    """Model for setting project secrets.

    Secrets are stored in project.config.secrets and synced to GitHub Actions.
    """

    project_id: str = Field(..., description="Project ID (UUID format)")

    key: str = Field(
        ...,
        pattern=r"^[A-Z_][A-Z0-9_]*$",
        description="Secret key - must be uppercase with underscores (e.g., TELEGRAM_TOKEN)",
        examples=["TELEGRAM_TOKEN", "DATABASE_URL", "API_KEY"],
    )

    value: str = Field(
        ...,
        min_length=1,
        description="Secret value",
    )
