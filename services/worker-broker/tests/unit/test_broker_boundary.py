import hashlib
import json

from fakeredis import FakeAsyncRedis
import httpx
import pytest

from shared.contracts.dto.commit_publication import (
    CommitPublication,
    PublicationFailure,
    publication_pending_key,
)
from shared.contracts.dto.run_result import EngineeringFailureReason
from shared.contracts.queues.worker_result import WorkerFailedResult
from shared.contracts.vocab import WorkerType
from shared.contracts.worker_control_plane import (
    GitHubCredentialRequest,
    WorkerControlPlaneOperation,
)
from shared.contracts.worker_turn import WorkerActiveTurn, active_turn_key
from src import main
from src.auth import credential_key, verify_token
from src.config import BrokerSettings


@pytest.mark.asyncio
async def test_refusal_park_failure_keeps_output_and_input_then_lost_reply_replays(monkeypatch):
    redis = FakeAsyncRedis(decode_responses=True)
    main.app.state.redis = redis
    worker_id, token = "publication-worker", "p" * 43
    registration = main.Registration(
        worker_id=worker_id,
        token=token,
        worker_type=WorkerType.DEVELOPER,
        input_stream="publication-input",
        output_stream="publication-output",
    )
    await main.register_worker(registration, main.settings.WORKER_BROKER_INTERNAL_TOKEN)
    await redis.xadd(
        registration.input_stream,
        {
            "data": json.dumps(
                {
                    "request_id": "publication-request",
                    "attempt_id": "eng-publication",
                    "turn_deadline_seconds": 60,
                    "prompt": "change the fixture",
                }
            )
        },
    )
    parked = False
    park_calls = []

    def upstream(request):
        if request.url.path.endswith("authorize"):
            return httpx.Response(200, json={"disposition": "eligible"})
        park_calls.append(request)
        if not parked:
            return httpx.Response(503)
        return httpx.Response(200, json=json.loads(request.content)["publication"])

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        main.httpx,
        "AsyncClient",
        lambda **kwargs: client_class(
            transport=httpx.MockTransport(upstream),
            **kwargs,
        ),
    )
    lease = await main.lease_input(worker_id, token)
    output = WorkerFailedResult(
        error="Non-force push refused",
        worker_report="The preserved change is ready",
        input_tokens=12,
        output_tokens=8,
        total_tokens=20,
        failure_reason=EngineeringFailureReason.WORKER_COMMIT_NOT_PUBLISHED,
        publication=CommitPublication(
            failure=PublicationFailure.PUSH_REFUSED,
            commit_sha="b" * 40,
            branch="story/fixture",
            stderr="remote rejected update",
        ),
    )
    submission = main.Submission(lease_id=lease["lease_id"], result=output.model_dump(mode="json"))
    with pytest.raises(httpx.HTTPStatusError):
        await main.submit_output(worker_id, submission, token)
    saved = json.loads(await redis.get(publication_pending_key("eng-publication")))
    assert saved["worker_report"] == output.worker_report and saved["total_tokens"] == 20
    assert saved["publication"]["attempt_id"] == "eng-publication"
    assert (await redis.xpending(registration.input_stream, registration.consumer_group))[
        "pending"
    ] == 1
    assert await redis.xlen(registration.output_stream) == 0
    parked = True
    assert await main.submit_output(worker_id, submission, token) == {"ok": True}
    assert await main.submit_output(worker_id, submission, token) == {"ok": True}
    assert len(park_calls) == 2
    assert await redis.xlen(registration.output_stream) == 1
    assert await redis.get(publication_pending_key("eng-publication")) is None
    assert (await redis.xpending(registration.input_stream, registration.consumer_group))[
        "pending"
    ] == 0


@pytest.mark.asyncio
async def test_stopped_queued_turn_never_creates_an_active_lease(monkeypatch):
    redis = FakeAsyncRedis(decode_responses=True)
    main.app.state.redis = redis
    token = "s" * 43
    await main.register_worker(
        main.Registration(
            worker_id="stopped",
            token=token,
            worker_type=WorkerType.DEVELOPER,
            input_stream="stopped-input",
            output_stream="stopped-output",
        ),
        main.settings.WORKER_BROKER_INTERNAL_TOKEN,
    )
    await redis.xadd(
        "stopped-input",
        {
            "data": json.dumps(
                {
                    "request_id": "stopped-request",
                    "attempt_id": "eng-stopped",
                    "turn_deadline_seconds": 60,
                    "prompt": "must not reach the executor",
                }
            )
        },
    )
    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        main.httpx,
        "AsyncClient",
        lambda **kwargs: client_class(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"disposition": "stopped"})
            ),
            **kwargs,
        ),
    )
    with pytest.raises(main.HTTPException) as denied:
        await main.lease_input("stopped", token)
    assert denied.value.status_code == 409
    assert await redis.hgetall(active_turn_key("stopped")) == {}
    assert await redis.xlen("stopped-output") == 0


def test_broker_internal_token_cannot_be_empty():
    with pytest.raises(ValueError, match="WORKER_BROKER_INTERNAL_TOKEN"):
        BrokerSettings(WORKER_BROKER_INTERNAL_TOKEN="")


def test_worker_credential_is_worker_scoped_and_constant_time_verifiable():
    token = "a" * 43
    stored = hashlib.sha256(token.encode()).hexdigest()

    assert credential_key("one") != credential_key("two")
    assert verify_token(token, stored)
    assert not verify_token("b" * 43, stored)


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_type", [WorkerType.QA, WorkerType.DEVELOPER])
async def test_credential_route_denies_before_forwarding(worker_type, monkeypatch):
    from unittest.mock import MagicMock

    redis = FakeAsyncRedis(decode_responses=True)
    main.app.state.redis = redis
    token = "x" * 43
    await main.register_worker(
        main.Registration(
            worker_id="owned",
            token=token,
            worker_type=worker_type,
            input_stream="in",
            output_stream="out",
        ),
        main.settings.WORKER_BROKER_INTERNAL_TOKEN,
    )
    forwarding = MagicMock(side_effect=AssertionError("forwarded denial"))
    monkeypatch.setattr(main.httpx, "AsyncClient", forwarding)
    with pytest.raises(main.HTTPException) as denial:
        await main.github_credential(
            "owned",
            GitHubCredentialRequest(repository="org/repo"),
            token if worker_type is WorkerType.QA else "wrong",
        )
    assert denial.value.status_code == 403
    forwarding.assert_not_called()


@pytest.mark.asyncio
async def test_credential_proxy_forwards_the_same_identity_and_typed_request(monkeypatch):
    redis = FakeAsyncRedis(decode_responses=True)
    main.app.state.redis = redis
    token = "x" * 43
    await main.register_worker(
        main.Registration(
            worker_id="owned",
            token=token,
            worker_type=WorkerType.DEVELOPER,
            input_stream="in",
            output_stream="out",
        ),
        main.settings.WORKER_BROKER_INTERNAL_TOKEN,
    )
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json={"token": "synthetic-current"})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        main.httpx,
        "AsyncClient",
        lambda **kwargs: client_class(transport=httpx.MockTransport(upstream), **kwargs),
    )
    response = await main.github_credential(
        "owned", GitHubCredentialRequest(repository="org/repo"), token
    )
    assert json.loads(response.body)["token"] == "synthetic-current"  # noqa: S105
    assert response.headers["cache-control"] == "no-store"
    assert calls[0].url.path == "/api/worker/owned/github/credential"
    assert calls[0].headers["X-Worker-Broker-Token"] == token
    assert json.loads(calls[0].content) == {"repository": "org/repo"}


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
            worker_type=WorkerType.DEVELOPER,
            input_stream="worker:one:input",
            output_stream="worker:one:output",
        ),
        main.settings.WORKER_BROKER_INTERNAL_TOKEN,
    )
    await main.register_worker(
        main.Registration(
            worker_id="two",
            token=token_two,
            worker_type=WorkerType.DEVELOPER,
            input_stream="worker:two:input",
            output_stream="worker:two:output",
        ),
        main.settings.WORKER_BROKER_INTERNAL_TOKEN,
    )

    with pytest.raises(main.HTTPException) as denied:
        await main._worker(redis, "two", token_one, WorkerControlPlaneOperation.INPUT_LEASE)
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
                worker_type=WorkerType.DEVELOPER,
                input_stream=f"worker:{worker_id}:input",
                output_stream=f"worker:{worker_id}:output",
                session_ttl_seconds=2,
            ),
            main.settings.WORKER_BROKER_INTERNAL_TOKEN,
        )

    await main.set_session("one", main.SessionUpdate(session_id="session-1"), token_one)
    assert 0 < await redis.ttl("worker:session:one") <= 2
    assert (await main.get_session("one", token_one))["session_id"] == "session-1"

    protected_paths = (
        lambda token: main.lease_input("two", token),
        lambda token: main.submit_output(
            "two", main.Submission(lease_id="1-0", result={"status": "failed", "error": "x"}), token
        ),
        lambda token: main.update_status(
            "two", main.StatusUpdate(values={"status": "running"}), token
        ),
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
    monkeypatch.setattr(main.settings, "WORKER_BROKER_STREAM_MAXLEN", 2)
    token = "a" * 43
    await main.register_worker(
        main.Registration(
            worker_id="one",
            token=token,
            worker_type=WorkerType.DEVELOPER,
            input_stream="worker:one:input",
            output_stream="worker:one:output",
        ),
        main.settings.WORKER_BROKER_INTERNAL_TOKEN,
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
        worker_type=WorkerType.DEVELOPER,
        input_stream=f"worker:{worker_id}:input",
        output_stream=f"worker:{worker_id}:output",
        session_ttl_seconds=60,
    )

    with pytest.raises(main.HTTPException) as denied:
        await main.register_worker(registration, "wrong-internal-token")
    assert denied.value.status_code == 403

    await main.register_worker(registration, main.settings.WORKER_BROKER_INTERNAL_TOKEN)
    await redis.xadd(
        registration.input_stream,
        {
            "data": json.dumps(
                {
                    "attempt_id": "eng-attempt-1",
                    "request_id": "request-1",
                    "turn_deadline_seconds": 4500,
                    "task_id": "task-1",
                    "prompt": "fix it",
                }
            )
        },
    )

    real_async_client = httpx.AsyncClient
    with monkeypatch.context() as authority:
        authority.setattr(
            main.httpx,
            "AsyncClient",
            lambda **kwargs: real_async_client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json={"disposition": "eligible"})
                ),
                **kwargs,
            ),
        )
        lease = await main.lease_input(worker_id, worker_token)
    active = WorkerActiveTurn.from_redis_fields(await redis.hgetall(active_turn_key(worker_id)))
    assert active is not None
    assert active.attempt_id == "eng-attempt-1"
    assert active.request_id == "request-1"
    assert active.lease_id == lease["lease_id"]

    await main.set_session(worker_id, main.SessionUpdate(session_id="session-1"), worker_token)
    assert await main.get_session(worker_id, worker_token) == {"session_id": "session-1"}
    await main.update_status(
        worker_id, main.StatusUpdate(values={"status": "running"}), worker_token
    )
    assert await redis.hgetall(f"worker:status:{worker_id}") == {"status": "running"}

    forwarded = {}
    client_timeouts = []

    def upstream(request: httpx.Request) -> httpx.Response:
        forwarded["url"] = str(request.url)
        forwarded["token"] = request.headers["X-Worker-Broker-Token"]
        forwarded["body"] = json.loads(request.content)
        return httpx.Response(400, json={"detail": "compose command rejected"})

    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(upstream)

    def client_factory(**kwargs):
        client_timeouts.append(kwargs.get("timeout"))
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(main.httpx, "AsyncClient", client_factory)

    compose_response = await main.compose(worker_id, {"args": ["up", "-d"]}, worker_token)
    assert compose_response.status_code == 400
    assert client_timeouts[-1] == 1740
    assert json.loads(compose_response.body) == {"detail": "compose command rejected"}
    assert forwarded == {
        "url": f"{main.settings.WORKER_MANAGER_URL}/api/worker/{worker_id}/infra/compose",
        "token": worker_token,
        "body": {"args": ["up", "-d"]},
    }

    await main.submit_output(
        worker_id,
        main.Submission(
            lease_id=lease["lease_id"], result={"status": "failed", "error": "agent failed"}
        ),
        worker_token,
    )
    output = await redis.xrange(registration.output_stream)
    assert output[0][1]["request_id"] == "request-1"
    result = json.loads(output[0][1]["data"])
    assert result["status"] == "failed"
    assert result["error"] == "agent failed"
    assert await redis.hgetall(active_turn_key(worker_id)) == {}


@pytest.mark.asyncio
async def test_lease_rejects_malformed_typed_turn_before_wrapper_execution():
    redis = FakeAsyncRedis(decode_responses=True)
    main.app.state.redis = redis
    worker_id = "typed-turn-worker"
    token = "t" * 43
    registration = main.Registration(
        worker_id=worker_id,
        token=token,
        worker_type=WorkerType.DEVELOPER,
        input_stream=f"worker:{worker_id}:input",
        output_stream=f"worker:{worker_id}:output",
    )
    await main.register_worker(registration, main.settings.WORKER_BROKER_INTERNAL_TOKEN)
    await redis.xadd(
        registration.input_stream,
        {
            "data": json.dumps(
                {
                    "request_id": "req-1",
                    "attempt_id": "attempt-1",
                    "prompt": "do the work",
                }
            )
        },
    )

    with pytest.raises(main.HTTPException) as invalid:
        await main.lease_input(worker_id, token)

    assert invalid.value.status_code == 422
    assert await redis.hgetall(active_turn_key(worker_id)) == {}


@pytest.mark.asyncio
async def test_a_qa_worker_gets_the_turn_protocol_and_no_control_plane(monkeypatch):
    """A QA executor's own credential runs its turn and buys nothing else.

    The refusal is on the operation, decided from the type the server recorded
    at registration — the worker never states its own type — and the upstream
    call is not made at all, so nothing reaches the management host's daemon.
    """
    redis = FakeAsyncRedis(decode_responses=True)
    main.app.state.redis = redis
    worker_id = "qa-executor"
    token = "q" * 43

    await main.register_worker(
        main.Registration(
            worker_id=worker_id,
            token=token,
            worker_type=WorkerType.QA,
            input_stream=f"worker:{worker_id}:input",
            output_stream=f"worker:{worker_id}:output",
            session_ttl_seconds=60,
        ),
        main.settings.WORKER_BROKER_INTERNAL_TOKEN,
    )
    assert await redis.hget(credential_key(worker_id), "worker_type") == WorkerType.QA.value

    # The turn protocol: everything a QA run actually needs.
    # This is the actual QA producer shape: it has a request id but no
    # engineering attempt/deadline, so it must not be rejected after XREADGROUP.
    await redis.xadd(
        f"worker:{worker_id}:input",
        {
            "data": json.dumps(
                {"request_id": "qa-request-1", "task_id": "qa-1", "prompt": "test it"}
            )
        },
    )
    lease = await main.lease_input(worker_id, token)
    assert lease["data"]["task_id"] == "qa-1"
    assert await redis.hgetall(active_turn_key(worker_id)) == {}
    await main.update_status(worker_id, main.StatusUpdate(values={"status": "running"}), token)
    await main.set_session(worker_id, main.SessionUpdate(session_id="qa-session"), token)
    assert (await main.get_session(worker_id, token))["session_id"] == "qa-session"
    await main.clear_session(worker_id, token)
    await main.submit_output(
        worker_id,
        main.Submission(
            lease_id=lease["lease_id"], result={"status": "failed", "error": "qa run aborted"}
        ),
        token,
    )

    # And the one thing that can build a container on the management host.
    def upstream(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"the broker forwarded a QA compose request to {request.url}")

    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(upstream)
    monkeypatch.setattr(
        main.httpx, "AsyncClient", lambda **kwargs: real_async_client(transport=transport, **kwargs)
    )

    with pytest.raises(main.HTTPException) as denied:
        await main.compose(worker_id, {"args": ["build"]}, token)
    assert denied.value.status_code == 403
    assert denied.value.detail == "a qa worker may not call infra.compose"


@pytest.mark.asyncio
async def test_a_credential_registered_without_a_recorded_type_is_refused_everything():
    """Fail closed: worker-manager records the type as it hands out the token."""
    redis = FakeAsyncRedis(decode_responses=True)
    main.app.state.redis = redis
    token = "u" * 43
    await redis.hset(
        credential_key("stray"),
        mapping={"token_digest": hashlib.sha256(token.encode()).hexdigest()},
    )

    for operation in WorkerControlPlaneOperation:
        with pytest.raises(main.HTTPException) as denied:
            await main._worker(redis, "stray", token, operation)
        assert denied.value.status_code == 403
        assert denied.value.detail == "worker type is not recorded for this worker"


def test_every_worker_route_states_the_operation_it_authorizes():
    """No route can reach Redis without naming what it lets a worker do.

    `_worker` takes the operation as a required argument, so a new route that
    forgets it does not silently inherit somebody else's permissions — it fails
    to call the authenticator at all.
    """
    import inspect

    parameters = inspect.signature(main._worker).parameters
    assert "operation" in parameters
    assert parameters["operation"].default is inspect.Parameter.empty
