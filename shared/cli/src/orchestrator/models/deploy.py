"""Pydantic models for deploy-related CLI commands."""

from pydantic import BaseModel, Field


class DeployStart(BaseModel):
    """Model for triggering deploy tasks.

    The CLI will auto-generate the task ID - agents only provide the project ID.
    """

    project_id: str = Field(
        ...,
        min_length=1,
        description="Project ID to deploy",
        examples=["abc-123", "550e8400-e29b-41d4-a716-446655440000"],
    )
