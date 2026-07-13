"""Typed schemas for API responses."""

from __future__ import annotations

from typing import TypedDict


class AllocationInfo(TypedDict, total=False):
    """Port allocation information from API."""

    id: int
    server_handle: str
    port: int
    application_id: int | None
    service_name: str
    server_ip: str | None


class ProjectConfig(TypedDict, total=False):
    """Project configuration dictionary."""

    secrets: dict[str, str]
    required_secrets: list[str]
    modules: list[str]
    detailed_spec: str


class ProjectInfo(TypedDict, total=False):
    """Project information from API."""

    id: str
    name: str
    status: str
    config: ProjectConfig
    project_spec: dict | None
    owner_id: int


class RepositoryInfo(TypedDict, total=False):
    """Repository information from API."""

    id: str
    project_id: str
    name: str
    git_url: str
    provider_repo_id: int | None
    role: str
    visibility: str
    is_managed: bool


class RepoInfo(TypedDict, total=False):
    """GitHub repository information."""

    name: str
    full_name: str
    html_url: str
    clone_url: str
