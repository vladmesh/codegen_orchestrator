"""HTTP endpoints for running docker compose commands on behalf of workers."""

import hashlib
import hmac

import structlog
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

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

    # ComposeRunner is the only policy compiler and executor. The router keeps
    # authentication and workspace lookup separate from policy decisions.
    stored_workspace = await redis.hget(f"worker:meta:{worker_id}", "workspace_path")
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
