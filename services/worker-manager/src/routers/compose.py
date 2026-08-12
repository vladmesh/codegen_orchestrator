"""HTTP endpoints for running docker compose commands on behalf of workers."""

import hashlib
import hmac

import structlog
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from shared.contracts.worker_control_plane import (
    WorkerControlPlaneOperation,
    control_plane_denial,
)
from shared.redis import decode_redis_fields

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
    redis = req.app.state.redis

    broker_metadata = await redis.hgetall(f"worker:broker:{worker_id}")
    expected = broker_metadata.get("token_digest") if broker_metadata else None
    digest = hashlib.sha256((x_worker_broker_token or "").encode()).hexdigest()
    if not expected or not hmac.compare_digest(digest, expected):
        raise HTTPException(status_code=403, detail="broker authentication required")

    # Authenticated is not authorized. The broker refuses this operation to a QA
    # executor too, and this is deliberately the same decision taken again on
    # this side of the hop: the token that reaches here is readable by the agent
    # inside the container (`/proc/<ppid>/environ`), so a caller holding it may
    # be the agent itself rather than its wrapper, and it may have skipped the
    # broker. The type comes from this service's own record of the worker it
    # created, written before the credential existed.
    meta = decode_redis_fields(await redis.hgetall(f"worker:meta:{worker_id}"))
    denial = control_plane_denial(meta.get("worker_type"), WorkerControlPlaneOperation.INFRA_COMPOSE)
    if denial:
        logger.warning(
            "worker_control_plane_operation_denied",
            worker_id=worker_id,
            operation=WorkerControlPlaneOperation.INFRA_COMPOSE.value,
            worker_type=meta.get("worker_type"),
            reason=denial,
        )
        raise HTTPException(status_code=403, detail=denial)

    # ComposeRunner is the only policy compiler and executor. The router keeps
    # authentication and workspace lookup separate from policy decisions.
    stored_workspace = meta.get("workspace_path")
    # Run compose
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
            workspace_path=stored_workspace,
        )
        raise

    return ComposeResponse(exit_code=exit_code, stdout=stdout, stderr=stderr)
