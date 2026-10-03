"""Repository credential issuance under the existing worker authority."""

import hashlib
import hmac
from urllib.parse import urlsplit

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import ValidationError
from starlette.responses import JSONResponse

from shared.contracts.dto.repository import RepositoryDTO
from shared.contracts.queues.worker import WorkerOwnership
from shared.contracts.worker_control_plane import (
    GitHubCredentialRequest,
    GitHubCredentialResponse,
    WorkerControlPlaneOperation,
    control_plane_denial,
)
from shared.redis import decode_redis_fields

router = APIRouter(prefix="/api/worker", tags=["credentials"])


@router.post("/{worker_id}/github/credential")
async def github_credential(
    worker_id: str,
    request: GitHubCredentialRequest,
    req: Request,
    x_worker_broker_token: str | None = Header(default=None),
) -> JSONResponse:
    state = req.app.state
    if not x_worker_broker_token:
        raise HTTPException(403, "broker authentication required")
    broker = decode_redis_fields(await state.redis.hgetall(f"worker:broker:{worker_id}"))
    digest = hashlib.sha256(x_worker_broker_token.encode()).hexdigest()
    if not broker.get("token_digest") or not hmac.compare_digest(digest, broker["token_digest"]):
        raise HTTPException(403, "broker authentication required")
    meta = decode_redis_fields(await state.redis.hgetall(f"worker:meta:{worker_id}"))
    denial = control_plane_denial(
        meta.get("worker_type"), WorkerControlPlaneOperation.GITHUB_CREDENTIAL
    )
    if denial:
        raise HTTPException(403, denial)
    try:
        owner = WorkerOwnership.model_validate(meta)
        repo_id = meta["repo_id"]
        if not repo_id:
            raise ValueError("missing repository")
        response = await state.credential_api.request("GET", f"repositories/{repo_id}")
        repository = RepositoryDTO.model_validate(response.json())
        url = urlsplit(repository.git_url)
        if repository.id != repo_id or str(repository.project_id) != owner.project_id:
            raise ValueError("mismatched ownership")
        if url.scheme != "https" or url.netloc != "github.com" or url.query or url.fragment:
            raise ValueError("invalid repository URL")
        full_name = url.path.removeprefix("/").removesuffix(".git")
        owned = GitHubCredentialRequest(repository=full_name)
        if request.repository != owned.repository:
            raise ValueError("repository differs from ownership")
    except (KeyError, ValueError, ValidationError):
        raise HTTPException(403, "repository ownership refused") from None
    except Exception:  # noqa: BLE001 - no API response or credential in diagnostics
        raise HTTPException(503, "repository ownership unavailable") from None
    github_owner, name = owned.repository.split("/")
    try:
        token = await state.github.get_repo_scoped_token(github_owner, name)
        result = GitHubCredentialResponse(token=token)
    except Exception:  # noqa: BLE001 - never return minting exception details
        raise HTTPException(503, "repository credential unavailable") from None
    # Explicit wire serialization: default SecretStr serialization is masked.
    # This route has no response model and never logs the response.
    return JSONResponse(
        {"token": result.token.get_secret_value()}, headers={"Cache-Control": "no-store"}
    )
