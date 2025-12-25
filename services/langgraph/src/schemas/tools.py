"""Pydantic schemas for LangGraph tool return values.

These schemas document what each tool returns, making it easier
for developers to understand the data structure without reading tool code.

Tools return these structures as dicts, but developers can reference
these schemas to understand the expected fields.
"""

from typing import Literal

from pydantic import BaseModel, Field


class ProjectIntent(BaseModel):
    """Intent from Product Owner."""

    intent: Literal["new_project", "update_project", "deploy", "maintenance"]
    summary: str | None = None
    project_id: str | None = None


class ProjectActivationResult(BaseModel):
    """Return value from `activate_project` tool.

    Contains information needed to set up a discovered project for deployment.
    """

    project_id: str = Field(..., description="Activated project ID")
    project_name: str = Field(..., description="Project name")
    status: str = Field("setup_required", description="New project status")
    required_secrets: list[str] = Field(
        default_factory=list, description="Env var names from .env.example"
    )
    missing_secrets: list[str] = Field(
        default_factory=list, description="Secrets not yet configured"
    )
    has_docker_compose: bool = Field(False, description="Whether project has docker-compose.yml")
    repo_info: dict | None = Field(
        None, description="Repository info (full_name, html_url, clone_url)"
    )

    # Error case
    error: str | None = Field(None, description="Error message if activation failed")


class RepositoryInspectionResult(BaseModel):
    """Return value from `inspect_repository` tool.

    Contains analysis of a project's GitHub repository for deployment readiness.
    """

    project_id: str = Field(..., description="Inspected project ID")
    required_secrets: list[str] = Field(
        default_factory=list, description="Env var names parsed from .env.example"
    )
    missing_secrets: list[str] = Field(
        default_factory=list, description="Secrets that need to be configured"
    )
    has_docker_compose: bool = Field(False, description="Whether docker-compose.yml exists")
    files: list[str] = Field(default_factory=list, description="Files in repo root (first 20)")

    # Error case
    error: str | None = Field(None, description="Error message if inspection failed")


class PortAllocationResult(BaseModel):
    """Return value from `allocate_port` tool.

    Contains details about the allocated port and server for deployment.
    """

    id: int | None = Field(None, description="Port allocation record ID")
    server_handle: str = Field(..., description="Server where port was allocated")
    server_ip: str = Field(..., description="Server public IP (crucial for DevOps)")
    port: int = Field(..., description="Allocated port number")
    service_name: str = Field(..., description="Service using this port")
    project_id: str = Field(..., description="Owning project ID")

    # Timestamps from API
    created_at: str | None = Field(None, description="Allocation timestamp")


class ServerSearchResult(BaseModel):
    """Return value from `find_suitable_server` tool.

    Contains server details including available resources.
    Returns None (as dict with error) if no suitable server found.
    """

    # Core fields (from ServerRead schema)
    handle: str = Field(..., description="Server handle (e.g., 'vps-267179')")
    host: str = Field(..., description="Hostname")
    public_ip: str = Field(..., description="Public IP address")
    status: str = Field(..., description="Server status")

    # Capacity
    capacity_ram_mb: int = Field(0, description="Total RAM in MB")
    capacity_disk_mb: int = Field(0, description="Total disk in MB")
    used_ram_mb: int = Field(0, description="Used RAM in MB")
    used_disk_mb: int = Field(0, description="Used disk in MB")

    # Computed by find_suitable_server
    available_ram_mb: int = Field(0, description="Available RAM (capacity - used)")
    available_disk_mb: int = Field(0, description="Available disk (capacity - used)")

    # Metadata
    labels: dict = Field(default_factory=dict, description="Server labels")
    is_managed: bool = Field(True, description="Whether server is managed")


class DeploymentReadinessResult(BaseModel):
    """Return value from `check_ready_to_deploy` tool.

    Indicates whether a project has met all deployment prerequisites.
    """

    ready: bool = Field(False, description="True if project can be deployed")
    project_id: str = Field(..., description="Checked project ID")
    project_name: str | None = Field(None, description="Project name")
    missing: list[str] = Field(
        default_factory=list, description="Missing requirements (secrets, etc.)"
    )
    has_docker_compose: bool = Field(False, description="Whether docker-compose.yml exists")

    # Error case
    error: str | None = Field(None, description="Error if check failed")


class SecretSaveResult(BaseModel):
    """Return value from `save_project_secret` tool."""

    saved: bool = Field(True, description="Whether secret was saved")
    key: str = Field(..., description="Secret key that was saved")
    project_id: str = Field(..., description="Project ID")
    missing_secrets: list[str] = Field(
        default_factory=list, description="Remaining missing secrets after save"
    )

    # Error case
    error: str | None = Field(None, description="Error if save failed")


class ProjectCreateResult(BaseModel):
    """Return value from `create_project` tool."""

    id: str = Field(..., description="Created project ID")
    name: str = Field(..., description="Project name")
    status: str = Field("pending_resources", description="Initial project status")
    config: dict = Field(default_factory=dict, description="Project configuration")
    created_at: str | None = Field(None, description="Creation timestamp")


class IncidentCreateResult(BaseModel):
    """Return value from `create_incident` tool."""

    id: int = Field(..., description="Incident ID")
    server_handle: str = Field(..., description="Affected server handle")
    incident_type: str = Field(..., description="Type of incident")
    status: str = Field("open", description="Incident status")
    details: dict = Field(default_factory=dict, description="Incident details")
    created_at: str | None = Field(None, description="Creation timestamp")


class GitHubRepoResult(BaseModel):
    """Return value from `create_github_repo` tool."""

    name: str = Field(..., description="Repository name")
    full_name: str = Field(..., description="Full repository name (org/repo)")
    html_url: str = Field(..., description="URL to repository")
    clone_url: str = Field(..., description="Clone URL")
    default_branch: str = Field("main", description="Default branch name")


class ProjectInfo(BaseModel):
    """Detailed project information."""

    id: str = Field(..., description="Project ID")
    name: str = Field(..., description="Project name")
    description: str | None = Field(None, description="Project description")
    status: str = Field(..., description="Project status")
    created_at: str | None = Field(None, description="Creation timestamp")
    repo_url: str | None = Field(None, description="Repository URL")
    is_active: bool = Field(True, description="Whether project is active")


class ServerInfo(BaseModel):
    """Detailed server information."""

    handle: str = Field(..., description="Server handle")
    host: str = Field(..., description="Hostname")
    public_ip: str = Field(..., description="Public IP address")
    status: str = Field(..., description="Server status")
    is_managed: bool = Field(True, description="Whether server is managed")
    capacity_ram_mb: int = Field(0, description="Total RAM")
    used_ram_mb: int = Field(0, description="Used RAM")


class IncidentInfo(BaseModel):
    """Detailed incident information."""

    id: int = Field(..., description="Incident ID")
    server_handle: str = Field(..., description="Related server handle")
    incident_type: str = Field(..., description="Type of incident")
    status: str = Field(..., description="Incident status")
    details: dict = Field(default_factory=dict, description="Incident details")
    created_at: str | None = Field(None, description="Creation timestamp")
