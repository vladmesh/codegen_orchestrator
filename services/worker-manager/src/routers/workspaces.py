"""Workspace introspection API — browse project workspaces by project_id.

Workspaces belong to projects (not workers). A single workspace is reused
across multiple worker runs for the same project.
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
    project_id: str
    path: str
    content: str
    size: int


def _resolve_workspace_path(request: Request, project_id: str) -> Path:
    """Resolve workspace path for a project.

    Checks WORKSPACE_BASE_PATH/{project_id}/workspace/ first,
    then falls back to SCAFFOLDED_WORKSPACE_PATH/{project_id}/.
    """
    workspace_base = request.app.state.workspace_base_path
    scaffolded_base = request.app.state.scaffolded_workspace_path

    # Primary: project workspace
    primary = Path(workspace_base) / project_id / "workspace"
    if primary.exists() and primary.is_dir():
        return primary

    # Fallback: scaffolded workspace (flat structure, no /workspace/ subdir)
    fallback = Path(scaffolded_base) / project_id
    if fallback.exists() and fallback.is_dir():
        return fallback

    raise HTTPException(
        status_code=HTTPStatus.NOT_FOUND,
        detail=f"No workspace found for project {project_id}",
    )


@router.get("/workspaces/{project_id}/tree", response_model=list[FileTreeEntry])
async def get_workspace_tree(project_id: str, request: Request):
    """List files in a project's workspace directory."""
    workspace = _resolve_workspace_path(request, project_id)
    return walk_workspace(workspace)


@router.get(
    "/workspaces/{project_id}/files/{file_path:path}",
    response_model=WorkspaceFileContentResponse,
)
async def get_workspace_file(project_id: str, file_path: str, request: Request):
    """Read a file from a project's workspace."""
    workspace = _resolve_workspace_path(request, project_id)
    content, size = read_file(workspace, file_path)
    return WorkspaceFileContentResponse(
        project_id=project_id,
        path=file_path,
        content=content,
        size=size,
    )
