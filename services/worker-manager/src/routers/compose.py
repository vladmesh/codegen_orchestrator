"""HTTP endpoints for running docker compose commands on behalf of workers."""

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..compose_validator import validate_command, validate_compose_file, resolve_compose_path
from ..compose_runner import ComposeRunner

logger = structlog.get_logger()

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

    # 2. Get compose runner and docker client from app state
    runner: ComposeRunner = req.app.state.compose_runner
    docker = req.app.state.docker
    redis = req.app.state.redis

    # Resolve actual workspace path from Redis metadata.
    # When workers are created with a project_id, the workspace lives under
    # the project_id directory, not the worker_id directory.
    stored_workspace = await redis.hget(f"worker:meta:{worker_id}", "workspace_path")

    # 3. Resolve and validate compose file(s)
    from pathlib import Path
    from ..config import settings

    workspace_path = (
        Path(stored_workspace) if stored_workspace else (Path(settings.WORKSPACE_BASE_PATH) / worker_id / "workspace")
    )
    container_name = f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}"

    # Collect compose file paths from -f/--file flags, or default to docker-compose.yml
    compose_files: list[str] = []
    args_iter = iter(request.args)
    for arg in args_iter:
        if arg in ("-f", "--file"):
            try:
                compose_files.append(next(args_iter))
            except StopIteration:
                break
    if not compose_files:
        compose_files = ["docker-compose.yml"]

    for cf in compose_files:
        # Check path traversal (works without filesystem access)
        _, path_result = resolve_compose_path(cf, workspace_path)
        if not path_result.valid:
            raise HTTPException(status_code=400, detail="; ".join(path_result.errors))

        # Read compose file from inside the worker container.
        # This works in DinD where the host filesystem doesn't reflect container writes.
        try:
            exit_code, output = await docker.exec_in_container(container_name, f"cat /workspace/{cf}", user="root")
            if exit_code == 0:
                file_result = validate_compose_file(output.decode())
                if not file_result.valid:
                    raise HTTPException(status_code=400, detail="; ".join(file_result.errors))
        except HTTPException:
            raise
        except Exception:
            pass  # Container unreachable or file missing — compose will fail naturally

    # Check path traversal in cwd
    _, cwd_result = resolve_compose_path(request.cwd, workspace_path)
    if not cwd_result.valid:
        raise HTTPException(status_code=400, detail="; ".join(cwd_result.errors))

    # 4. Run compose
    try:
        exit_code, stdout, stderr = await runner.run(
            worker_id=worker_id,
            args=request.args,
            cwd=request.cwd,
            timeout=request.timeout,
            workspace_dir=stored_workspace,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception(
            "compose_run_failed",
            worker_id=worker_id,
            args=request.args,
            cwd=request.cwd,
            workspace_path=str(workspace_path),
        )
        raise

    return ComposeResponse(exit_code=exit_code, stdout=stdout, stderr=stderr)
