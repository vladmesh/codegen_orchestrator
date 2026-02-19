"""HTTP endpoints for running docker compose commands on behalf of workers."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..compose_validator import validate_command, validate_compose_file, resolve_compose_path
from ..compose_runner import ComposeRunner

router = APIRouter(prefix="/api/worker", tags=["compose"])


class ComposeRequest(BaseModel):
    args: list[str]
    cwd: str = "."
    timeout: int = 120


class ComposeResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str


@router.post("/{worker_id}/infra/compose")
async def run_compose(worker_id: str, request: ComposeRequest, req: Request) -> ComposeResponse:
    """Run a docker compose command scoped to a worker's workspace."""
    # 1. Validate command whitelist and flags
    cmd_result = validate_command(request.args)
    if not cmd_result.valid:
        raise HTTPException(status_code=400, detail="; ".join(cmd_result.errors))

    # 2. Get compose runner from app state
    runner: ComposeRunner = req.app.state.compose_runner

    # 3. Resolve and validate compose file if present
    from pathlib import Path
    from ..config import settings

    workspace_path = Path(settings.WORKSPACE_BASE_PATH) / worker_id / "workspace"
    if workspace_path.exists():
        compose_file = workspace_path / "docker-compose.yml"
        if compose_file.exists():
            file_result = validate_compose_file(compose_file.read_text())
            if not file_result.valid:
                raise HTTPException(status_code=400, detail="; ".join(file_result.errors))

        # Check path traversal in cwd
        _, path_result = resolve_compose_path(request.cwd, workspace_path)
        if not path_result.valid:
            raise HTTPException(status_code=400, detail="; ".join(path_result.errors))

    # 4. Run compose
    try:
        exit_code, stdout, stderr = await runner.run(
            worker_id=worker_id,
            args=request.args,
            cwd=request.cwd,
            timeout=request.timeout,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return ComposeResponse(exit_code=exit_code, stdout=stdout, stderr=stderr)
