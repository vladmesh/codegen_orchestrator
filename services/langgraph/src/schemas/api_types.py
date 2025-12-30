"""Typed schemas for API responses.

These TypedDicts provide type safety for dict data returned from the API service.
Use these instead of raw dict access to catch type errors at development time.
"""

from __future__ import annotations

from typing import TypedDict


class ServerInfo(TypedDict, total=False):
    """Server information from API."""

    handle: str
    host: str
    public_ip: str | None
    ssh_user: str
    status: str
    is_managed: bool
    capacity_ram_mb: int
    used_ram_mb: int
    available_ram_mb: int
    os_template: str | None


class AllocationInfo(TypedDict, total=False):
    """Port allocation information from API."""

    id: int
    server_handle: str
    port: int
    project_id: str | None
    service_name: str
    # Enriched fields (added by tools)
    server_ip: str | None


class ProjectConfig(TypedDict, total=False):
    """Project configuration dict."""

    secrets: dict[str, str]
    required_secrets: list[str]
    repository_url: str
    modules: list[str]
    detailed_spec: str


class ProjectInfo(TypedDict, total=False):
    """Project information from API."""

    id: str
    name: str
    status: str
    repository_url: str | None
    github_repo_id: int | None
    config: ProjectConfig
    project_spec: dict | None
    owner_id: int | None
    user_id: int | None  # Alias used in some contexts


class RepoInfo(TypedDict, total=False):
    """Repository information."""

    name: str
    full_name: str
    html_url: str
    clone_url: str


def get_server_ip(server: ServerInfo) -> str | None:
    """Get server IP with fallback to host."""
    return server.get("public_ip") or server.get("host")


def get_repo_url(project: ProjectInfo) -> str | None:
    """Get repository URL from project, checking both locations."""
    return project.get("repository_url") or (project.get("config") or {}).get("repository_url")
