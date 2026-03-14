"""Pydantic schemas for deployments (formerly service_deployments)."""

from datetime import datetime
import uuid

from pydantic import BaseModel

from shared.contracts.dto.base import TimestampedDTO
from shared.contracts.dto.deployment import DeploymentResult


class DeploymentBase(BaseModel):
    """Base schema for deployment."""

    project_id: uuid.UUID
    service_name: str
    server_handle: str
    port: int
    deployment_info: dict = {}


class DeploymentCreate(DeploymentBase):
    """Schema for creating a deployment record."""

    application_id: int | None = None
    result: str = DeploymentResult.PENDING.value
    deployed_sha: str | None = None


class DeploymentUpdate(BaseModel):
    """Schema for updating a deployment record."""

    result: str | None = None
    deployment_info: dict | None = None
    deployed_sha: str | None = None


class DeploymentRead(DeploymentBase, TimestampedDTO):
    """Schema for reading a deployment record."""

    id: int
    application_id: int | None = None
    result: str
    deployed_sha: str | None = None
    deployed_at: datetime


# Backward compat aliases
ServiceDeploymentCreate = DeploymentCreate
ServiceDeploymentRead = DeploymentRead
ServiceDeploymentUpdate = DeploymentUpdate
