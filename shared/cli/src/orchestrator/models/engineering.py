"""Pydantic models for engineering-related CLI commands."""

from pydantic import BaseModel, Field


class EngineeringTask(BaseModel):
    """Model for triggering engineering tasks.

    The CLI will auto-generate the task ID - agents only provide the project ID.
    """

    project_id: str = Field(
        ...,
        min_length=1,
        description="Project ID to run engineering task for",
        examples=["abc-123", "550e8400-e29b-41d4-a716-446655440000"],
    )
