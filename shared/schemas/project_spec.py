"""Project specification schema for .project-spec.yaml files.

This schema defines the machine-readable project specification format
that is stored in each project repository for disaster recovery.
"""

from pydantic import BaseModel, Field


class ProjectInfo(BaseModel):
    """Basic project information."""

    name: str = Field(..., description="Project name in snake_case")
    description: str = Field(..., description="Brief project description")


class EntryPointSpec(BaseModel):
    """Entry point specification."""

    type: str = Field(..., description="Entry point type: telegram_bot, api, frontend")
    handlers: list[str] | None = Field(None, description="Handler names for telegram bots")
    port: int | None = Field(None, description="Port for API/frontend")


class InfrastructureSpec(BaseModel):
    """Infrastructure requirements."""

    min_ram_mb: int = Field(default=256, description="Minimum RAM in MB")
    min_disk_mb: int = Field(default=512, description="Minimum disk space in MB")
    ports: list[int] = Field(default_factory=list, description="Required ports")


class ProjectSpecYAML(BaseModel):
    """Machine-readable project specification for .project-spec.yaml.

    This is the source of truth for project metadata and enables
    disaster recovery by storing all essential project information
    in the repository itself.
    """

    version: str = Field(default="1.0", description="Spec format version")
    project: ProjectInfo = Field(..., description="Project metadata")
    modules: list[str] = Field(..., description="Service template modules to use")
    entry_points: list[EntryPointSpec] = Field(..., description="Project entry points")
    secrets_required: list[str] = Field(
        default_factory=list, description="Required environment secrets"
    )
    infrastructure: InfrastructureSpec = Field(
        default_factory=InfrastructureSpec, description="Infrastructure requirements"
    )

    def to_yaml_dict(self) -> dict:
        """Convert to dict suitable for YAML serialization."""
        return self.model_dump(exclude_none=True, by_alias=True)
