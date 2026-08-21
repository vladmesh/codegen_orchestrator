"""The only worker-network service allowed to bridge worker control traffic."""

import json
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog
from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from shared.contracts.queues.worker_result import parse_worker_result
from shared.contracts.worker_turn import WorkerActiveTurn, active_turn_key
from shared.contracts.vocab import WorkerType
from shared.contracts.worker_control_plane import (
    WorkerControlPlaneOperation,
    control_plane_denial,
)
from shared.worker_type_cutover import backfill_pre_cutover_worker_type

from .auth import credential_key, token_digest, verify_token
from .config import settings

logger = structlog.get_logger(__name__)


class Registration(BaseModel):
    worker_id: str
    token: str = Field(min_length=32)
    # What kind of worker this credential belongs to. It arrives on the internal
    # endpoint, which only worker-manager can call, and it is stored next to the
    # token digest — so every later authorization reads the server's record of
    # the worker and never anything the worker says about itself.
    worker_type: WorkerType
    input_stream: str
    output_stream: str
    consumer_group: str = "worker_group"
    session_ttl_seconds: int = Field(default=settings.SESSION_TTL_SECONDS, gt=0)


class Submission(BaseModel):
    lease_id: str
    result: dict[str, Any]


class StatusUpdate(BaseModel):
    values: dict[str, str]


class SessionUpdate(BaseModel):
    session_id: str


def _decode(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _active_turn(worker_id: str, lease_id: str, data: dict[str, Any]) -> WorkerActiveTurn | None:
    """Build a record for the versioned engineering-turn payload only.

    QA and pre-turn-protocol workers legitimately send a request id without an
    engineering attempt.  They stay compatible and lease normally; only the
    producer of the complete three-field protocol creates supervision state.
    """
    if data.get("attempt_id") is None or data.get("turn_deadline_seconds") is None:
        return None
    if data.get("request_id") is None:
        # Treat malformed producer payload as legacy rather than poisoning an
        # already-delivered stream entry.  Engineering producers always write
        # the complete identity before XADD.
        return None
    try:
        deadline_seconds = int(data["turn_deadline_seconds"])
    except (TypeError, ValueError):
        raise HTTPException(422, "worker input has an invalid turn deadline") from None
    if deadline_seconds <= 0:
        raise HTTPException(422, "worker input has an invalid turn deadline")
    now = datetime.now(UTC)
    return WorkerActiveTurn(
        worker_id=worker_id,
        attempt_id=str(data["attempt_id"]),
        request_id=str(data["request_id"]),
        lease_id=lease_id,
        started_at=now,
        deadline_at=now + timedelta(seconds=deadline_seconds),
    )


async def _worker(
    redis: Redis,
    worker_id: str,
    token: str | None,
    operation: WorkerControlPlaneOperation,
) -> dict[str, str]:
    """Authenticate a worker credential and authorize the operation it names.

    Every worker route goes through here and every route must say which
    operation it is: a new route cannot be added without stating what it lets a
    worker do, because there is no way to call this without saying it.
    """
    if not token:
        raise HTTPException(401, "missing worker credential")
    metadata = await redis.hgetall(credential_key(worker_id))
    metadata = {_decode(k): _decode(v) for k, v in metadata.items()}
    if not verify_token(token, metadata.get("token_digest")):
        raise HTTPException(403, "invalid worker credential")

    denial = control_plane_denial(metadata.get("worker_type"), operation)
    if denial:
        logger.warning(
            "worker_control_plane_operation_denied",
            worker_id=worker_id,
            operation=operation.value,
            worker_type=metadata.get("worker_type"),
            reason=denial,
        )
        raise HTTPException(403, denial)
    return metadata


def _internal(token: str | None) -> None:
    if not token or not secrets.compare_digest(token, settings.BROKER_INTERNAL_TOKEN):
        raise HTTPException(403, "invalid broker internal credential")


async def migrate_pre_cutover_credentials(redis: Redis) -> int:
    """Name the type of credentials issued before the type was recorded.

    Runs before this process serves a request, so a developer worker that was
    mid-turn when the broker restarted keeps its channel. See
    `shared/worker_type_cutover.py` for why `developer` is a proof here and not
    a guess, and for when this call is due to be deleted.
    """
    return await backfill_pre_cutover_worker_type(redis, key_pattern=credential_key("*"), boundary="worker-broker")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    await migrate_pre_cutover_credentials(app.state.redis)
    yield
    await app.state.redis.aclose()


app = FastAPI(title="worker-broker", lifespan=lifespan)


@app.post("/internal/workers")
async def register_worker(registration: Registration, x_broker_internal_token: str | None = Header(default=None)):
    _internal(x_broker_internal_token)
    redis: Redis = app.state.redis
    await redis.hset(
        credential_key(registration.worker_id),
        mapping={
            "token_digest": token_digest(registration.token),
            "worker_type": registration.worker_type.value,
            "input_stream": registration.input_stream,
            "output_stream": registration.output_stream,
            "consumer_group": registration.consumer_group,
            "session_ttl_seconds": str(registration.session_ttl_seconds),
        },
    )
    try:
        await redis.xgroup_create(registration.input_stream, registration.consumer_group, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise
    return {"ok": True}


@app.delete("/internal/workers/{worker_id}")
async def unregister_worker(worker_id: str, x_broker_internal_token: str | None = Header(default=None)):
    _internal(x_broker_internal_token)
    await app.state.redis.delete(credential_key(worker_id), f"worker:session:{worker_id}", active_turn_key(worker_id))
    return {"ok": True}


@app.post("/v1/workers/{worker_id}/input/lease")
async def lease_input(worker_id: str, x_worker_broker_token: str | None = Header(default=None)):
    redis: Redis = app.state.redis
    metadata = await _worker(redis, worker_id, x_worker_broker_token, WorkerControlPlaneOperation.INPUT_LEASE)
    entries = await redis.xreadgroup(
        metadata["consumer_group"], worker_id, {metadata["input_stream"]: ">"}, count=1, block=1
    )
    if not entries:
        return Response(status_code=204)
    _, messages = entries[0]
    message_id, fields = messages[0]
    decoded = {_decode(k): _decode(v) for k, v in fields.items()}
    if set(decoded) == {"data"}:
        try:
            decoded = json.loads(decoded["data"])
        except json.JSONDecodeError:
            raise HTTPException(422, "invalid worker input payload") from None
    lease_id = _decode(message_id)
    active_turn = _active_turn(worker_id, lease_id, decoded)
    if active_turn is not None:
        await redis.hset(active_turn_key(worker_id), mapping=active_turn.as_redis_fields())
    return {"lease_id": lease_id, "data": decoded}


@app.post("/v1/workers/{worker_id}/output")
async def submit_output(
    worker_id: str, submission: Submission, x_worker_broker_token: str | None = Header(default=None)
):
    redis: Redis = app.state.redis
    metadata = await _worker(redis, worker_id, x_worker_broker_token, WorkerControlPlaneOperation.OUTPUT_SUBMIT)
    result = parse_worker_result(submission.result)
    await redis.xadd(
        metadata["output_stream"],
        {"data": json.dumps(result.model_dump(mode="json"))},
        maxlen=settings.STREAM_MAXLEN,
        approximate=True,
    )
    # ACK comes after the typed output is durably accepted. A failed submission leaves the input pending.
    await redis.xack(metadata["input_stream"], metadata["consumer_group"], submission.lease_id)
    active_turn = WorkerActiveTurn.from_redis_fields(await redis.hgetall(active_turn_key(worker_id)))
    if active_turn is not None and active_turn.lease_id == submission.lease_id:
        await redis.delete(active_turn_key(worker_id))
    return {"ok": True}


@app.post("/v1/workers/{worker_id}/status")
async def update_status(worker_id: str, update: StatusUpdate, x_worker_broker_token: str | None = Header(default=None)):
    redis: Redis = app.state.redis
    await _worker(redis, worker_id, x_worker_broker_token, WorkerControlPlaneOperation.STATUS_UPDATE)
    await redis.hset(f"worker:status:{worker_id}", mapping=update.values)
    return {"ok": True}


@app.get("/v1/workers/{worker_id}/session")
async def get_session(worker_id: str, x_worker_broker_token: str | None = Header(default=None)):
    redis: Redis = app.state.redis
    metadata = await _worker(redis, worker_id, x_worker_broker_token, WorkerControlPlaneOperation.SESSION_READ)
    value = await redis.get(f"worker:session:{worker_id}")
    if value:
        await redis.expire(f"worker:session:{worker_id}", int(metadata["session_ttl_seconds"]))
    return {"session_id": value}


@app.put("/v1/workers/{worker_id}/session")
async def set_session(worker_id: str, update: SessionUpdate, x_worker_broker_token: str | None = Header(default=None)):
    redis: Redis = app.state.redis
    metadata = await _worker(redis, worker_id, x_worker_broker_token, WorkerControlPlaneOperation.SESSION_WRITE)
    await redis.set(f"worker:session:{worker_id}", update.session_id, ex=int(metadata["session_ttl_seconds"]))
    return {"ok": True}


@app.delete("/v1/workers/{worker_id}/session")
async def clear_session(worker_id: str, x_worker_broker_token: str | None = Header(default=None)):
    redis: Redis = app.state.redis
    await _worker(redis, worker_id, x_worker_broker_token, WorkerControlPlaneOperation.SESSION_CLEAR)
    await redis.delete(f"worker:session:{worker_id}")
    return {"ok": True}


@app.post("/v1/workers/{worker_id}/infra/compose")
async def compose(worker_id: str, request: dict[str, Any], x_worker_broker_token: str | None = Header(default=None)):
    redis: Redis = app.state.redis
    await _worker(redis, worker_id, x_worker_broker_token, WorkerControlPlaneOperation.INFRA_COMPOSE)
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            f"{settings.WORKER_MANAGER_URL}/api/worker/{worker_id}/infra/compose",
            json=request,
            headers={"X-Worker-Broker-Token": x_worker_broker_token or ""},
        )
    try:
        body = response.json()
    except json.JSONDecodeError:
        body = {"error": "worker-manager returned invalid JSON"}
    return Response(content=json.dumps(body), status_code=response.status_code, media_type="application/json")
