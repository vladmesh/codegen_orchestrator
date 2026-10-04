"""Broker requests are checked against worker-manager and locked API ownership."""

import hashlib
import hmac

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from shared.contracts.queues.worker import WorkerOwnership
from shared.contracts.queues.worker_result import WorkerFailedResult
from shared.contracts.worker_turn import WorkerActiveTurn, active_turn_key
from shared.redis import decode_redis_fields

router = APIRouter(prefix="/api/worker", tags=["engineering authority"])


class AttemptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: str = Field(min_length=1)


async def owned_worker(state, worker_id, token):
    broker = decode_redis_fields(await state.redis.hgetall(f"worker:broker:{worker_id}"))
    digest = hashlib.sha256((token or "").encode()).hexdigest()
    if not broker.get("token_digest") or not hmac.compare_digest(digest, broker["token_digest"]):
        raise HTTPException(403, "broker authentication required")
    meta = decode_redis_fields(await state.redis.hgetall(f"worker:meta:{worker_id}"))
    if meta.get("worker_type") != "developer":
        raise HTTPException(403, "developer ownership required")
    return WorkerOwnership.model_validate(meta)


@router.post("/{worker_id}/engineering/authorize")
async def authorize_turn(
    worker_id: str,
    command: AttemptRequest,
    req: Request,
    x_worker_broker_token: str | None = Header(default=None),
):
    owner = await owned_worker(req.app.state, worker_id, x_worker_broker_token)
    response = await req.app.state.credential_api.request(
        "POST",
        f"runs/{command.attempt_id}/engineering-disposition",
        json={},
    )
    decision = response.json()
    if decision["project_id"] != owner.project_id or decision["story_id"] != owner.story_id:
        raise HTTPException(403, "attempt ownership refused")
    return decision


@router.post("/{worker_id}/engineering/park-publication")
async def park_publication(
    worker_id: str,
    output: WorkerFailedResult,
    req: Request,
    x_worker_broker_token: str | None = Header(default=None),
):
    state = req.app.state
    publication = output.publication
    if publication is None:
        raise HTTPException(422, "typed publication refusal required")
    owner = await owned_worker(state, worker_id, x_worker_broker_token)
    active = WorkerActiveTurn.from_redis_fields(
        decode_redis_fields(await state.redis.hgetall(active_turn_key(worker_id)))
    )
    if (
        active is None
        or publication.attempt_id != active.attempt_id
        or publication.worker_id != worker_id
    ):
        raise HTTPException(409, "active attempt ownership refused")
    run_response = await state.credential_api.request("GET", f"runs/{active.attempt_id}")
    run = run_response.json()
    if str(run["project_id"]) != owner.project_id or run["story_id"] != owner.story_id:
        raise HTTPException(403, "attempt ownership refused")
    meta = decode_redis_fields(await state.redis.hgetall(f"worker:meta:{worker_id}"))
    repository_response = await state.credential_api.request(
        "GET", f"repositories/{meta['repo_id']}"
    )
    repository = repository_response.json()
    if str(repository["project_id"]) != owner.project_id:
        raise HTTPException(403, "repository ownership refused")
    publication = publication.model_copy(
        update={
            "repository_id": repository["id"],
            "repository_url": repository["git_url"],
        }
    )
    response = await state.credential_api.request(
        "POST",
        f"runs/{active.attempt_id}/park-publication",
        json=output.model_copy(update={"publication": publication}).model_dump(mode="json"),
    )
    return response.json()
