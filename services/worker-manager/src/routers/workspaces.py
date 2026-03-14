"""Workspace introspection API — browse workspaces by repo_id.

Workspaces are created by the scaffolder service at SCAFFOLDED_WORKSPACE_PATH/{repo_id}/.
They are reused across multiple worker runs for the same repository.
"""

from http import HTTPStatus
from pathlib import Path

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ._shared import FileTreeEntry, read_file, walk_workspace

logger = structlog.get_logger()

router = APIRouter(prefix="/api/introspect", tags=["workspaces"])


class WorkspaceFileContentResponse(BaseModel):
    repo_id: str
    path: str
    content: str
    size: int


def _resolve_workspace_path(request: Request, repo_id: str) -> Path:
    """Resolve workspace path for a repository.

    Looks in SCAFFOLDED_WORKSPACE_PATH/{repo_id}/.
    """
    scaffolded_base = request.app.state.scaffolded_workspace_path
    workspace = Path(scaffolded_base) / repo_id
    if workspace.exists() and workspace.is_dir():
        return workspace

    raise HTTPException(
        status_code=HTTPStatus.NOT_FOUND,
        detail=f"No workspace found for repo {repo_id}",
    )


@router.get("/workspaces/{repo_id}/tree", response_model=list[FileTreeEntry])
async def get_workspace_tree(repo_id: str, request: Request):
    """List files in a repository's workspace directory."""
    workspace = _resolve_workspace_path(request, repo_id)
    return walk_workspace(workspace)


@router.get(
    "/workspaces/{repo_id}/files/{file_path:path}",
    response_model=WorkspaceFileContentResponse,
)
async def get_workspace_file(repo_id: str, file_path: str, request: Request):
    """Read a file from a repository's workspace."""
    workspace = _resolve_workspace_path(request, repo_id)
    content, size = read_file(workspace, file_path)
    return WorkspaceFileContentResponse(
        repo_id=repo_id,
        path=file_path,
        content=content,
        size=size,
    )
