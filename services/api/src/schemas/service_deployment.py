"""Pydantic schemas for service deployments."""

from datetime import datetime
import uuid

from pydantic import BaseModel

from shared.contracts.dto.base import TimestampedDTO


class ServiceDeploymentBase(BaseModel):
    """Base schema for service deployment."""

    project_id: uuid.UUID
    service_name: str
    server_handle: str
    port: int
    deployment_info: dict = {}


class ServiceDeploymentCreate(ServiceDeploymentBase):
    """Schema for creating a service deployment."""

    status: str = "running"
    deployed_sha: str | None = None


class ServiceDeploymentUpdate(BaseModel):
    """Schema for updating a service deployment."""

    status: str | None = None
    deployment_info: dict | None = None
    deployed_sha: str | None = None


class ServiceDeploymentRead(ServiceDeploymentBase, TimestampedDTO):
    """Schema for reading a service deployment."""

    id: int
    status: str
    deployed_sha: str | None = None
    deployed_at: datetime
