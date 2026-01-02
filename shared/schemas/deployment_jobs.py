"""Deployment job schemas for infrastructure-worker.

These schemas define the contract between langgraph DeployerNode
and infrastructure-worker for delegated Ansible deployment execution.
"""

from __future__ import annotations

from typing import TypedDict


class DeploymentJobRequest(TypedDict, total=False):
    """Request schema for delegating deployment to infrastructure-worker.

    Sent from langgraph DeployerNode to infrastructure-worker via Redis queue.
    """

    job_type: str  # "deploy"
    request_id: str  # Unique request ID for polling result
    project_id: str  # Project ID from database
    project_name: str  # Normalized project name (snake_case)
    repo_full_name: str  # GitHub repo "owner/repo"
    github_token: str  # GitHub PAT for cloning private repos
    server_ip: str  # Target server IP
    port: int  # Allocated port for service
    secrets: dict[str, str]  # Resolved environment variables
    modules: str | None  # Comma-separated modules (e.g., "backend,frontend")
    callback_stream: str | None  # Optional Redis stream for progress events


class DeploymentJobResult(TypedDict, total=False):
    """Result schema from infrastructure-worker deployment execution.

    Written to Redis key `deploy:result:{request_id}` after completion.
    """

    status: str  # "success" | "failed" | "error"
    deployed_url: str | None  # URL where service is accessible (if success)
    server_ip: str | None  # Server IP (repeated for convenience)
    port: int | None  # Service port (repeated for convenience)
    error: str | None  # Error message (if failed/error)
    stdout: str | None  # Ansible stdout (if failed)
    stderr: str | None  # Ansible stderr (if failed)
    exit_code: int | None  # Ansible exit code (if failed)
