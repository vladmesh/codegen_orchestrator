"""Service deployment model for tracking deployed services on servers."""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ServiceDeployment(Base):
    """Track service deployments on servers for recovery and management."""
    
    __tablename__ = "service_deployments"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    
    # Project and service identification
    project_id: Mapped[str] = mapped_column(index=True)
    service_name: Mapped[str]
    
    # Server reference
    server_handle: Mapped[str] = mapped_column(
        ForeignKey("servers.handle", ondelete="CASCADE"),
        index=True
    )
    
    # Resource allocation
    port: Mapped[int]
    
    # Deployment metadata
    deployed_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow,
        onupdate=datetime.utcnow
    )
    
    # Status: "running", "stopped", "failed"
    status: Mapped[str] = mapped_column(default="running", index=True)
    
    # Deployment information for redeployment
    # Expected keys: repo_url, branch, docker_compose_path, env_vars, etc.
    deployment_info: Mapped[dict] = mapped_column(JSON, default=dict)
    
    def __repr__(self) -> str:
        return f"<ServiceDeployment(id={self.id}, project={self.project_id}, service={self.service_name}, server={self.server_handle}, status={self.status})>"
