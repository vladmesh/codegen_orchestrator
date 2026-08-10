"""HTTP endpoints for running docker compose commands on behalf of workers."""

import hashlib
import hmac
import shlex

import structlog
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from ..compose_validator import (
    CONTAINER_CREATING_COMMANDS,
    VALUE_FLAGS,
    resolve_compose_path,
    validate_command,
    validate_compose_file,
    validate_effective_compose,
)
from ..compose_runner import ComposeRunner, _DEFAULT_COMPOSE_FILES

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


def _subcommand(args: list[str]) -> str | None:
    """Return the Compose subcommand using the same global-flag parsing as validation."""
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in VALUE_FLAGS:
            skip_next = True
            continue
        if not arg.startswith("-"):
            return arg
    return None


@router.post("/{worker_id}/infra/compose")
async def run_compose(
    worker_id: str,
    request: ComposeRequest,
    req: Request,
    x_worker_broker_token: str | None = Header(default=None),
) -> ComposeResponse:
    """Run a docker compose command scoped to a worker's workspace."""
    # Authenticate before validating request details so direct callers cannot
    # probe worker-scoped Compose policy.
    runner: ComposeRunner = req.app.state.compose_runner
    docker = req.app.state.docker
    redis = req.app.state.redis

    broker_metadata = await redis.hgetall(f"worker:broker:{worker_id}")
    expected = broker_metadata.get("token_digest") if broker_metadata else None
    digest = hashlib.sha256((x_worker_broker_token or "").encode()).hexdigest()
    if not expected or not hmac.compare_digest(digest, expected):
        raise HTTPException(status_code=403, detail="broker authentication required")

    # Validate command whitelist and flags only for the authenticated worker.
    cmd_result = validate_command(request.args)
    if not cmd_result.valid:
        raise HTTPException(status_code=400, detail="; ".join(cmd_result.errors))

    # Resolve actual workspace path from Redis metadata.
    # When workers are created with a project_id, the workspace lives under
    # the project_id directory, not the worker_id directory.
    stored_workspace = await redis.hget(f"worker:meta:{worker_id}", "workspace_path")

    # Resolve and validate the selected sources. These must be readable from the
    # worker before the host-side Compose CLI is permitted to resolve or execute
    # them. A missing container, unreadable file or malformed source is a policy
    # failure, never a reason to fall through to ComposeRunner.
    from pathlib import Path
    from ..config import settings

    workspace_path = (
        Path(stored_workspace) if stored_workspace else (Path(settings.SCAFFOLDED_WORKSPACE_PATH) / worker_id)
    )
    container_name = f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}"

    # Collect compose file paths from -f/--file flags, or default to infra/ layout
    compose_files: list[str] = []
    args_iter = iter(request.args)
    for arg in args_iter:
        if arg in ("-f", "--file"):
            try:
                compose_files.append(next(args_iter))
            except StopIteration:
                break
    if not compose_files:
        compose_files = list(_DEFAULT_COMPOSE_FILES)

    for cf in compose_files:
        # Check path traversal (works without filesystem access)
        _, path_result = resolve_compose_path(cf, workspace_path)
        if not path_result.valid:
            raise HTTPException(status_code=400, detail="; ".join(path_result.errors))

        source_path = f"/workspace/{cf}"
        try:
            exit_code, output = await docker.exec_in_container(
                container_name, f"cat -- {shlex.quote(source_path)}", user="root"
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Compose source '{cf}' cannot be read") from exc
        if exit_code != 0:
            raise HTTPException(status_code=400, detail=f"Compose source '{cf}' cannot be read")
        try:
            content = output.decode()
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"Compose source '{cf}' is not UTF-8") from exc
        file_result = validate_compose_file(content)
        if not file_result.valid:
            raise HTTPException(status_code=400, detail="; ".join(file_result.errors))

    # Check path traversal in cwd
    _, cwd_result = resolve_compose_path(request.cwd, workspace_path)
    if not cwd_result.valid:
        raise HTTPException(status_code=400, detail="; ".join(cwd_result.errors))

    # Compose itself resolves extends, interpolation and overrides. Resolve the
    # exact final JSON with the same fixed project name, environment, network and
    # ports override that ComposeRunner will execute. This is required even for
    # read-only commands so an unresolvable selected configuration cannot reach
    # the executor.
    try:
        effective_project, prepared = await runner.inspect(
            worker_id=worker_id,
            args=request.args,
            cwd=request.cwd,
            timeout=request.timeout,
            workspace_dir=stored_workspace,
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if _subcommand(request.args) in CONTAINER_CREATING_COMMANDS:
        effective_result = validate_effective_compose(effective_project, worker_id)
        if not effective_result.valid:
            raise HTTPException(status_code=400, detail="; ".join(effective_result.errors))

    # Run compose
    try:
        exit_code, stdout, stderr = await runner.run(
            worker_id=worker_id,
            args=request.args,
            cwd=request.cwd,
            timeout=request.timeout,
            workspace_dir=stored_workspace,
            prepared=prepared,
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
