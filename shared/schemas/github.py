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


class GitHubInstallation(BaseModel):
    """GitHub App installation info.

    Returned from GET /app/installations or /repos/{owner}/{repo}/installation.
    """

    model_config = ConfigDict(extra="allow")

    id: int = Field(..., description="Installation ID (used for generating tokens)")
    account: GitHubAccount = Field(..., description="Account where app is installed")
    app_id: int = Field(..., description="GitHub App ID")
    target_type: str | None = Field(None, description="Target type: 'User' or 'Organization'")
    permissions: dict | None = Field(None, description="Granted permissions")
    created_at: datetime | None = Field(None, description="Installation creation date")
    updated_at: datetime | None = Field(None, description="Last update date")


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


class GitHubFileContent(BaseModel):
    """File content from GET /repos/{owner}/{repo}/contents/{path}.

    Note: When requesting with Accept: application/vnd.github.raw+json,
    the API returns raw content as text. This schema is for JSON responses.
    """

    model_config = ConfigDict(extra="allow")

    name: str = Field(..., description="File name")
    path: str = Field(..., description="Path within repository")
    sha: str = Field(..., description="Git blob SHA")
    size: int = Field(..., description="File size in bytes")
    type: str = Field(..., description="Type: 'file', 'dir', or 'symlink'")

    # Content (only for files, base64 encoded)
    content: str | None = Field(None, description="Base64 encoded file content")
    encoding: str | None = Field(None, description="Content encoding (usually 'base64')")

    # URLs
    url: str | None = Field(None, description="API URL for this content")
    html_url: str | None = Field(None, description="Web URL for this file")
    download_url: str | None = Field(None, description="Raw download URL")


class GitHubContentItem(BaseModel):
    """Directory listing item from GET /repos/{owner}/{repo}/contents/{path}.

    When path is a directory, the API returns a list of these items.
    """

    model_config = ConfigDict(extra="allow")

    name: str = Field(..., description="File/directory name")
    path: str = Field(..., description="Path within repository")
    sha: str = Field(..., description="Git object SHA")
    size: int = Field(0, description="Size in bytes (0 for directories)")
    type: str = Field(..., description="Type: 'file', 'dir', or 'symlink'")
    url: str | None = Field(None, description="API URL")
    html_url: str | None = Field(None, description="Web URL")
    download_url: str | None = Field(None, description="Raw download URL (files only)")
