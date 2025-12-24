"""Pydantic schemas for service deployments."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ServiceDeploymentBase(BaseModel):
    """Base schema for service deployment."""
    
    project_id: str
    service_name: str
    server_handle: str
    port: int
    deployment_info: dict = {}


class ServiceDeploymentCreate(ServiceDeploymentBase):
    """Schema for creating a service deployment."""
    
    status: str = "running"


class ServiceDeploymentUpdate(BaseModel):
    """Schema for updating a service deployment."""
    
    status: str | None = None
    deployment_info: dict | None = None


class ServiceDeploymentRead(ServiceDeploymentBase):
    """Schema for reading a service deployment."""
    
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    status: str
    deployed_at: datetime
    updated_at: datetime
