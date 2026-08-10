import hashlib
import json

import httpx
import pytest
from fakeredis import FakeAsyncRedis

from src.auth import credential_key, verify_token
from src import main
from src.config import BrokerSettings


def test_broker_internal_token_cannot_be_empty():
    with pytest.raises(ValueError, match="BROKER_INTERNAL_TOKEN"):
        BrokerSettings(BROKER_INTERNAL_TOKEN="")


def test_worker_credential_is_worker_scoped_and_constant_time_verifiable():
    token = "a" * 43
    stored = hashlib.sha256(token.encode()).hexdigest()

    assert credential_key("one") != credential_key("two")
    assert verify_token(token, stored)
    assert not verify_token("b" * 43, stored)


@pytest.mark.asyncio
async def test_worker_credentials_cannot_cross_worker_boundaries():
    redis = FakeAsyncRedis(decode_responses=True)
    main.app.state.redis = redis
    token_one = "a" * 43
    token_two = "b" * 43

    await main.register_worker(
        main.Registration(
            worker_id="one",
            token=token_one,
            input_stream="worker:one:input",
            output_stream="worker:one:output",
        ),
        main.settings.BROKER_INTERNAL_TOKEN,
    )
    await main.register_worker(
        main.Registration(
            worker_id="two",
            token=token_two,
            input_stream="worker:two:input",
            output_stream="worker:two:output",
        ),
        main.settings.BROKER_INTERNAL_TOKEN,
    )

    with pytest.raises(main.HTTPException) as denied:
        await main._worker(redis, "two", token_one)
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_session_expiry_and_all_worker_paths_require_scoped_credentials(monkeypatch):
    redis = FakeAsyncRedis(decode_responses=True)
    main.app.state.redis = redis
    token_one = "a" * 43
    token_two = "b" * 43

    for worker_id, token in (("one", token_one), ("two", token_two)):
        await main.register_worker(
            main.Registration(
                worker_id=worker_id,
                token=token,
                input_stream=f"worker:{worker_id}:input",
                output_stream=f"worker:{worker_id}:output",
                session_ttl_seconds=2,
            ),
            main.settings.BROKER_INTERNAL_TOKEN,
        )

    await main.set_session("one", main.SessionUpdate(session_id="session-1"), token_one)
    assert 0 < await redis.ttl("worker:session:one") <= 2
    assert (await main.get_session("one", token_one))["session_id"] == "session-1"

    protected_paths = (
        lambda token: main.lease_input("two", token),
        lambda token: main.submit_output(
            "two", main.Submission(lease_id="1-0", result={"status": "failed", "error": "x"}), token
        ),
        lambda token: main.update_status("two", main.StatusUpdate(values={"status": "running"}), token),
        lambda token: main.get_session("two", token),
        lambda token: main.set_session("two", main.SessionUpdate(session_id="x"), token),
        lambda token: main.clear_session("two", token),
        lambda token: main.compose("two", {"args": ["ps"]}, token),
    )
    for call in protected_paths:
        with pytest.raises(main.HTTPException) as denied:
            await call(token_one)
        assert denied.value.status_code == 403

    with pytest.raises(main.HTTPException) as missing:
        await main.lease_input("one", None)
    assert missing.value.status_code == 401


@pytest.mark.asyncio
async def test_output_stream_retention_is_bounded(monkeypatch):
    redis = FakeAsyncRedis(decode_responses=True)
    main.app.state.redis = redis
    monkeypatch.setattr(main.settings, "STREAM_MAXLEN", 2)
    token = "a" * 43
    await main.register_worker(
        main.Registration(
            worker_id="one",
            token=token,
            input_stream="worker:one:input",
            output_stream="worker:one:output",
        ),
        main.settings.BROKER_INTERNAL_TOKEN,
    )
    for index in range(3):
        await main.submit_output(
            "one",
            main.Submission(
                lease_id=f"{index}-0",
                result={"status": "failed", "error": f"failure-{index}"},
            ),
            token,
        )

    assert await redis.xlen("worker:one:output") <= 2


@pytest.mark.asyncio
async def test_authenticated_registration_lease_output_session_and_compose_forward(monkeypatch):
    """Exercise every broker hop with the same credentials a worker receives."""
    redis = FakeAsyncRedis(decode_responses=True)
    main.app.state.redis = redis
    worker_id = "worker-one"
    worker_token = "a" * 43
    registration = main.Registration(
        worker_id=worker_id,
        token=worker_token,
        input_stream=f"worker:{worker_id}:input",
        output_stream=f"worker:{worker_id}:output",
        session_ttl_seconds=60,
    )

    with pytest.raises(main.HTTPException) as denied:
        await main.register_worker(registration, "wrong-internal-token")
    assert denied.value.status_code == 403

    await main.register_worker(registration, main.settings.BROKER_INTERNAL_TOKEN)
    await redis.xadd(registration.input_stream, {"data": json.dumps({"task_id": "task-1", "prompt": "fix it"})})

    lease = await main.lease_input(worker_id, worker_token)
    assert lease["data"] == {"task_id": "task-1", "prompt": "fix it"}

    await main.set_session(worker_id, main.SessionUpdate(session_id="session-1"), worker_token)
    assert await main.get_session(worker_id, worker_token) == {"session_id": "session-1"}
    await main.update_status(worker_id, main.StatusUpdate(values={"status": "running"}), worker_token)
    assert await redis.hgetall(f"worker:status:{worker_id}") == {"status": "running"}

    forwarded = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        forwarded["url"] = str(request.url)
        forwarded["token"] = request.headers["X-Worker-Broker-Token"]
        forwarded["body"] = json.loads(request.content)
        return httpx.Response(400, json={"detail": "compose command rejected"})

    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(upstream)
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: real_async_client(transport=transport, **kwargs))

    compose_response = await main.compose(worker_id, {"args": ["up", "-d"]}, worker_token)
    assert compose_response.status_code == 400
    assert json.loads(compose_response.body) == {"detail": "compose command rejected"}
    assert forwarded == {
        "url": f"{main.settings.WORKER_MANAGER_URL}/api/worker/{worker_id}/infra/compose",
        "token": worker_token,
        "body": {"args": ["up", "-d"]},
    }

    await main.submit_output(
        worker_id,
        main.Submission(lease_id=lease["lease_id"], result={"status": "failed", "error": "agent failed"}),
        worker_token,
    )
    output = await redis.xrange(registration.output_stream)
    result = json.loads(output[0][1]["data"])
    assert result["status"] == "failed"
    assert result["error"] == "agent failed"
