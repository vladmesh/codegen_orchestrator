"""Pydantic schemas for GitHub API responses.

These schemas document the structure of GitHub API responses,
providing type safety for data received from the GitHub App API.

GitHub API Documentation: https://docs.github.com/en/rest
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class GitHubAccount(BaseModel):
    """GitHub account (user or organization)."""

    model_config = ConfigDict(extra="allow")

    login: str = Field(..., description="Account username/org name")
    id: int = Field(..., description="Numeric account ID")
    type: str = Field(..., description="Account type: 'User' or 'Organization'")
    avatar_url: str | None = Field(None, description="Avatar URL")
    html_url: str | None = Field(None, description="Profile page URL")


class GitHubRepository(BaseModel):
    """GitHub repository info.

    Returned from POST /orgs/{org}/repos (create) or GET /repos/{owner}/{repo}.
    """

    model_config = ConfigDict(extra="allow")

    id: int = Field(..., description="Repository ID")
    name: str = Field(..., description="Repository name")
    full_name: str = Field(..., description="Full name: owner/repo")
    private: bool = Field(True, description="Whether repo is private")
    description: str | None = Field(None, description="Repository description")

    # URLs
    html_url: str = Field(..., description="Web URL for the repository")
    clone_url: str = Field(..., description="HTTPS clone URL")
    ssh_url: str | None = Field(None, description="SSH clone URL")
    git_url: str | None = Field(None, description="Git protocol URL")

    # Owner
    owner: GitHubAccount | None = Field(None, description="Repository owner")

    # Metadata
    default_branch: str = Field("main", description="Default branch name")
    created_at: datetime | None = Field(None, description="Creation timestamp")
    updated_at: datetime | None = Field(None, description="Last update timestamp")
    pushed_at: datetime | None = Field(None, description="Last push timestamp")
    size: int | None = Field(None, description="Repository size in KB")

    # Flags
    fork: bool = Field(False, description="Whether this is a fork")
    archived: bool = Field(False, description="Whether repo is archived")
    disabled: bool = Field(False, description="Whether repo is disabled")
