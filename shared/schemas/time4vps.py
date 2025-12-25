"""Pydantic schemas for Time4VPS API responses.

These schemas document the structure of Time4VPS API responses,
providing type safety and validation for data received from the API.

API Documentation: https://billing.time4vps.com/api
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Time4VPSServer(BaseModel):
    """Server list item from GET /api/server.

    This is the minimal server info returned when listing all servers.
    Use `Time4VPSServerDetails` for full server information.
    """

    model_config = ConfigDict(extra="allow")

    id: int = Field(..., description="Unique server ID in Time4VPS")
    name: str | None = Field(None, description="Server display name")
    domain: str | None = Field(None, description="Server hostname/domain")
    ip: str | None = Field(None, description="Primary IP address")
    status: str | None = Field(None, description="Server status (active, suspended, etc)")
    product_name: str | None = Field(None, description="Product/plan name")


class Time4VPSServerDetails(BaseModel):
    """Detailed server info from GET /api/server/{server_id}.

    Contains full server specifications including OS, resources, and network info.
    """

    model_config = ConfigDict(extra="allow")

    id: int = Field(..., description="Unique server ID")
    name: str | None = Field(None, description="Server display name")
    domain: str | None = Field(None, description="Server hostname")
    ip: str | None = Field(None, description="Primary IP address")
    status: str | None = Field(None, description="Server status")

    # Resources
    ram_mb: int | None = Field(None, description="RAM in MB")
    disk_gb: int | None = Field(None, description="Disk space in GB")
    cpu_cores: int | None = Field(None, description="Number of CPU cores")
    bandwidth_gb: int | None = Field(None, description="Monthly bandwidth in GB")

    # OS Info
    os: str | None = Field(None, description="Current OS template name")
    os_name: str | None = Field(None, description="OS display name")

    # Network
    ipv6: str | None = Field(None, description="IPv6 address if assigned")

    # Dates
    created_at: datetime | None = Field(None, description="Server creation date")
    expires_at: datetime | None = Field(None, description="Service expiration date")


class Time4VPSTask(BaseModel):
    """Task status from GET /api/server/{server_id}/task/{task_id}.

    Used for tracking async operations like password reset or OS reinstall.
    """

    model_config = ConfigDict(extra="allow")

    name: str = Field(..., description="Task name (e.g., 'Reset Root Password')")
    activated: str = Field(..., description="ISO timestamp when task was activated")
    assigned: str | None = Field(None, description="ISO timestamp when task was assigned to worker")
    completed: str | None = Field(None, description="ISO timestamp when task completed (empty if pending)")
    results: str | None = Field(None, description="Task result string (may contain password in HTML)")

    @property
    def is_completed(self) -> bool:
        """Check if task has completed."""
        return bool(self.completed)


class Time4VPSOSTemplate(BaseModel):
    """Available OS template from GET /api/server/{server_id}/oses.

    Used for selecting OS during server reinstall.
    """

    model_config = ConfigDict(extra="allow")

    name: str = Field(..., description="Template identifier (e.g., 'kvm-ubuntu-24.04-gpt-x86_64')")
    title: str | None = Field(None, description="Human-readable OS name")
    arch: str | None = Field(None, description="Architecture (x86_64, etc)")
    version: str | None = Field(None, description="OS version")
