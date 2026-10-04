"""The only worker-network service allowed to bridge worker control traffic."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
import secrets
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Response
import httpx
from pydantic import BaseModel, Field, ValidationError
from redis.asyncio import Redis
from redis.exceptions import ResponseError
import structlog

from shared.contracts.queues.worker_result import parse_worker_result
from shared.contracts.vocab import WorkerType
from shared.contracts.worker_control_plane import (
    GitHubCredentialRequest,
    GitHubCredentialResponse,
    WorkerControlPlaneOperation,
    control_plane_denial,
)
from shared.contracts.worker_turn import WorkerActiveTurn, WorkerTurnInput, active_turn_key

from .auth import credential_key, token_digest, verify_token
from .config import settings

_ACCEPT_OUTPUT = """
local saved = redis.call('GET', KEYS[1])
if saved then
    if saved ~= ARGV[1] then return redis.error_reply('worker_output_replaced') end
    return saved
end
local fields = cjson.decode(ARGV[2])
local args = {}
for key, value in pairs(fields) do
    table.insert(args, key)
    table.insert(args, value)
end
redis.call('XADD', KEYS[2], 'MAXLEN', '~', ARGV[3], '*', unpack(args))
redis.call('XACK', KEYS[3], ARGV[4], ARGV[5])
if redis.call('HGET', KEYS[4], 'lease_id') == ARGV[5] then redis.call('DEL', KEYS[4]) end
if KEYS[5] ~= '' then redis.call('DEL', KEYS[5]) end
redis.call('SET', KEYS[1], ARGV[1])
return ARGV[1]
"""

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
    session_ttl_seconds: int = Field(default=settings.WORKER_BROKER_SESSION_TTL_SECONDS, gt=0)


class Submission(BaseModel):
    lease_id: str
    result: dict[str, Any]


class StatusUpdate(BaseModel):
    values: dict[str, str]


class SessionUpdate(BaseModel):
    session_id: str


def _decode(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _active_turn(worker_id: str, lease_id: str, turn: WorkerTurnInput) -> WorkerActiveTurn | None:
    """Build supervision state only for typed engineering turns."""
    if turn.attempt_id is None:
        return None
    now = datetime.now(UTC)
    return WorkerActiveTurn(
        worker_id=worker_id,
        attempt_id=turn.attempt_id,
        request_id=turn.request_id,
        lease_id=lease_id,
        started_at=now,
        deadline_at=now + timedelta(seconds=turn.turn_deadline_seconds),
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
    if not token or not secrets.compare_digest(token, settings.WORKER_BROKER_INTERNAL_TOKEN):
        raise HTTPException(403, "invalid broker internal credential")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    yield
    await app.state.redis.aclose()


app = FastAPI(title="worker-broker", lifespan=lifespan)


@app.post("/internal/workers")
async def register_worker(
    registration: Registration, x_broker_internal_token: str | None = Header(default=None)
):
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
        await redis.xgroup_create(
            registration.input_stream, registration.consumer_group, id="0", mkstream=True
        )
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise
    return {"ok": True}


@app.delete("/internal/workers/{worker_id}")
async def unregister_worker(
    worker_id: str, x_broker_internal_token: str | None = Header(default=None)
):
    _internal(x_broker_internal_token)
    await app.state.redis.delete(
        credential_key(worker_id), f"worker:session:{worker_id}", active_turn_key(worker_id)
    )
    return {"ok": True}


@app.post("/v1/workers/{worker_id}/input/lease")
async def lease_input(worker_id: str, x_worker_broker_token: str | None = Header(default=None)):
    redis: Redis = app.state.redis
    metadata = await _worker(
        redis, worker_id, x_worker_broker_token, WorkerControlPlaneOperation.INPUT_LEASE
    )
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
    try:
        turn = WorkerTurnInput.model_validate(decoded)
    except ValidationError as error:
        raise HTTPException(422, "invalid worker input payload") from error
    if turn.attempt_id is not None:
        async with httpx.AsyncClient(timeout=15) as client:
            decision = await client.post(
                f"{settings.WORKER_MANAGER_URL}/api/worker/{worker_id}/engineering/authorize",
                headers={"X-Worker-Broker-Token": x_worker_broker_token},
                json={"attempt_id": turn.attempt_id},
            )
            decision.raise_for_status()
        if decision.json()["disposition"] != "eligible":
            # Keep the input pending; stop/recovery owns it, never another turn.
            raise HTTPException(409, "engineering attempt is fenced")
    lease_id = _decode(message_id)
    active_turn = _active_turn(worker_id, lease_id, turn)
    if active_turn is not None:
        await redis.hset(active_turn_key(worker_id), mapping=active_turn.as_redis_fields())
    return {
        "lease_id": lease_id,
        "data": turn.model_dump(mode="json", exclude_none=True),
    }


@app.post("/v1/workers/{worker_id}/output")
async def submit_output(
    worker_id: str, submission: Submission, x_worker_broker_token: str | None = Header(default=None)
):
    redis: Redis = app.state.redis
    metadata = await _worker(
        redis, worker_id, x_worker_broker_token, WorkerControlPlaneOperation.OUTPUT_SUBMIT
    )
    result = parse_worker_result(submission.result)
    receipt_key = f"worker:output-receipt:{worker_id}:{submission.lease_id}"
    signature = hashlib.sha256(result.model_dump_json().encode()).hexdigest()
    saved = await redis.get(receipt_key)
    if saved is not None:
        if _decode(saved) != signature:
            raise HTTPException(409, "worker output already accepted different content")
        return {"ok": True}
    active_turn = WorkerActiveTurn.from_redis_fields(
        await redis.hgetall(active_turn_key(worker_id))
    )
    output_fields = {"data": json.dumps(result.model_dump(mode="json"))}
    if active_turn is not None and active_turn.lease_id == submission.lease_id:
        # The consumer that owns a reclaimed engineering entry must be able to
        # distinguish this turn from an older result retained on the same
        # reusable worker stream.  The request identity is broker-owned: the
        # wrapper cannot choose it in its result payload.
        output_fields["request_id"] = active_turn.request_id
    if getattr(result, "publication", None) is not None:
        if active_turn is None or active_turn.lease_id != submission.lease_id:
            raise HTTPException(409, "publication requires the active attempt lease")
        publication = result.publication.model_copy(
            update={
                "worker_id": worker_id,
                "attempt_id": active_turn.attempt_id,
            }
        )
        result = result.model_copy(update={"publication": publication})
        output_fields["data"] = result.model_dump_json()
        from shared.contracts.dto.commit_publication import publication_pending_key

        # Preserve evidence before an unavailable API can interrupt parking.
        # Timeout/removal settlement must consume this marker, never classify
        # the already-produced commit as an ordinary paid failure.
        await redis.set(publication_pending_key(active_turn.attempt_id), result.model_dump_json())
        # Durable park precedes output delivery and ACK. Failure leaves the
        # leased input and exact output reclaimable without admitting paid retry.
        async with httpx.AsyncClient(timeout=30) as client:
            parked = await client.post(
                f"{settings.WORKER_MANAGER_URL}/api/worker/{worker_id}/engineering/park-publication",
                headers={"X-Worker-Broker-Token": x_worker_broker_token},
                json=result.model_dump(mode="json"),
            )
            parked.raise_for_status()
        from shared.contracts.dto.commit_publication import CommitPublication

        result = result.model_copy(
            update={"publication": CommitPublication.model_validate(parked.json())}
        )
        output_fields["data"] = result.model_dump_json()
    from shared.contracts.dto.commit_publication import publication_pending_key

    # Output, ACK and replay receipt are atomic. A lost HTTP reply can be
    # replayed with the same body without another outcome or publication.
    await redis.eval(
        _ACCEPT_OUTPUT,
        5,
        receipt_key,
        metadata["output_stream"],
        metadata["input_stream"],
        active_turn_key(worker_id),
        publication_pending_key(active_turn.attempt_id) if active_turn else "",
        signature,
        json.dumps(output_fields),
        settings.WORKER_BROKER_STREAM_MAXLEN,
        metadata["consumer_group"],
        submission.lease_id,
    )
    return {"ok": True}


@app.post("/v1/workers/{worker_id}/status")
async def update_status(
    worker_id: str, update: StatusUpdate, x_worker_broker_token: str | None = Header(default=None)
):
    redis: Redis = app.state.redis
    await _worker(
        redis, worker_id, x_worker_broker_token, WorkerControlPlaneOperation.STATUS_UPDATE
    )
    await redis.hset(f"worker:status:{worker_id}", mapping=update.values)
    return {"ok": True}


@app.get("/v1/workers/{worker_id}/session")
async def get_session(worker_id: str, x_worker_broker_token: str | None = Header(default=None)):
    redis: Redis = app.state.redis
    metadata = await _worker(
        redis, worker_id, x_worker_broker_token, WorkerControlPlaneOperation.SESSION_READ
    )
    value = await redis.get(f"worker:session:{worker_id}")
    if value:
        await redis.expire(f"worker:session:{worker_id}", int(metadata["session_ttl_seconds"]))
    return {"session_id": value}


@app.put("/v1/workers/{worker_id}/session")
async def set_session(
    worker_id: str, update: SessionUpdate, x_worker_broker_token: str | None = Header(default=None)
):
    redis: Redis = app.state.redis
    metadata = await _worker(
        redis, worker_id, x_worker_broker_token, WorkerControlPlaneOperation.SESSION_WRITE
    )
    await redis.set(
        f"worker:session:{worker_id}", update.session_id, ex=int(metadata["session_ttl_seconds"])
    )
    return {"ok": True}


@app.delete("/v1/workers/{worker_id}/session")
async def clear_session(worker_id: str, x_worker_broker_token: str | None = Header(default=None)):
    redis: Redis = app.state.redis
    await _worker(
        redis, worker_id, x_worker_broker_token, WorkerControlPlaneOperation.SESSION_CLEAR
    )
    await redis.delete(f"worker:session:{worker_id}")
    return {"ok": True}


@app.post("/v1/workers/{worker_id}/infra/compose")
async def compose(
    worker_id: str,
    request: dict[str, Any],
    x_worker_broker_token: str | None = Header(default=None),
):
    redis: Redis = app.state.redis
    await _worker(
        redis, worker_id, x_worker_broker_token, WorkerControlPlaneOperation.INFRA_COMPOSE
    )
    # Worker-manager may run compose config + execution, each with an 840s ceiling.
    # Keep this hop below the wrapper shim's 1800s deadline but above both phases.
    async with httpx.AsyncClient(timeout=1740) as client:
        response = await client.post(
            f"{settings.WORKER_MANAGER_URL}/api/worker/{worker_id}/infra/compose",
            json=request,
            headers={"X-Worker-Broker-Token": x_worker_broker_token or ""},
        )
    try:
        body = response.json()
    except json.JSONDecodeError:
        body = {"error": "worker-manager returned invalid JSON"}
    return Response(
        content=json.dumps(body), status_code=response.status_code, media_type="application/json"
    )


@app.post("/v1/workers/{worker_id}/github/credential")
async def github_credential(
    worker_id: str,
    request: GitHubCredentialRequest,
    x_worker_broker_token: str | None = Header(default=None),
):
    await _worker(
        app.state.redis,
        worker_id,
        x_worker_broker_token,
        WorkerControlPlaneOperation.GITHUB_CREDENTIAL,
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{settings.WORKER_MANAGER_URL}/api/worker/{worker_id}/github/credential",
                json=request.model_dump(mode="json"),
                headers={"X-Worker-Broker-Token": x_worker_broker_token or ""},
            )
        response.raise_for_status()
        credential = GitHubCredentialResponse.model_validate(response.json())
    except httpx.HTTPStatusError as exc:
        raise HTTPException(exc.response.status_code, "repository credential refused") from None
    except Exception:  # noqa: BLE001 - payload-free transport refusal
        raise HTTPException(503, "repository credential unavailable") from None
    return Response(
        content=json.dumps({"token": credential.token.get_secret_value()}),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )
