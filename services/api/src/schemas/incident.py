"""Incident schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class IncidentBase(BaseModel):
    """Base incident schema."""
    
    server_handle: str = Field(description="Server handle")
    incident_type: str = Field(description="Type of incident")
    details: dict = Field(default_factory=dict, description="Additional details")
    affected_services: list = Field(default_factory=list, description="List of affected services")


class IncidentCreate(IncidentBase):
    """Schema for creating an incident."""
    pass


class IncidentUpdate(BaseModel):
    """Schema for updating an incident."""
    
    status: str | None = None
    resolved_at: datetime | None = None
    details: dict | None = None
    recovery_attempts: int | None = None


class IncidentRead(IncidentBase):
    """Schema for reading an incident."""
    
    id: int
    status: str
    detected_at: datetime
    resolved_at: datetime | None
    recovery_attempts: int
    model_config = ConfigDict(from_attributes=True)
