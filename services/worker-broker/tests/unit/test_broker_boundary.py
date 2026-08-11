import hashlib
import json

import httpx
import pytest
from fakeredis import FakeAsyncRedis

from shared.contracts.vocab import WorkerType
from shared.contracts.worker_control_plane import WorkerControlPlaneOperation

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
            worker_type=WorkerType.DEVELOPER,
            input_stream="worker:one:input",
            output_stream="worker:one:output",
        ),
        main.settings.BROKER_INTERNAL_TOKEN,
    )
    await main.register_worker(
        main.Registration(
            worker_id="two",
            token=token_two,
            worker_type=WorkerType.DEVELOPER,
            input_stream="worker:two:input",
            output_stream="worker:two:output",
        ),
        main.settings.BROKER_INTERNAL_TOKEN,
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
            worker_type=WorkerType.DEVELOPER,
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
        worker_type=WorkerType.DEVELOPER,
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
        main.settings.BROKER_INTERNAL_TOKEN,
    )
    assert await redis.hget(credential_key(worker_id), "worker_type") == WorkerType.QA.value

    # The turn protocol: everything a QA run actually needs.
    await redis.xadd(f"worker:{worker_id}:input", {"data": json.dumps({"task_id": "qa-1", "prompt": "test it"})})
    lease = await main.lease_input(worker_id, token)
    assert lease["data"]["task_id"] == "qa-1"
    await main.update_status(worker_id, main.StatusUpdate(values={"status": "running"}), token)
    await main.set_session(worker_id, main.SessionUpdate(session_id="qa-session"), token)
    assert (await main.get_session(worker_id, token))["session_id"] == "qa-session"
    await main.clear_session(worker_id, token)
    await main.submit_output(
        worker_id,
        main.Submission(lease_id=lease["lease_id"], result={"status": "failed", "error": "qa run aborted"}),
        token,
    )

    # And the one thing that can build a container on the management host.
    def upstream(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"the broker forwarded a QA compose request to {request.url}")

    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(upstream)
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: real_async_client(transport=transport, **kwargs))

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
    await redis.hset(credential_key("stray"), mapping={"token_digest": hashlib.sha256(token.encode()).hexdigest()})

    for operation in WorkerControlPlaneOperation:
        with pytest.raises(main.HTTPException) as denied:
            await main._worker(redis, "stray", token, operation)
        assert denied.value.status_code == 403
        assert denied.value.detail == "worker type is not recorded for this worker"


async def _pre_cutover_credential(redis, worker_id: str, token: str) -> None:
    """Register a worker the way the previous release did: with no `worker_type`.

    Copied field for field from `services/worker-broker/src/main.py` at
    `fdeaa770`, the base this branch was reviewed against — including the
    consumer group it created, because a worker mid-turn has one.
    """
    await redis.hset(
        credential_key(worker_id),
        mapping={
            "token_digest": hashlib.sha256(token.encode()).hexdigest(),
            "input_stream": f"worker:{worker_id}:input",
            "output_stream": f"worker:{worker_id}:output",
            "consumer_group": "worker_group",
            "session_ttl_seconds": "3600",
        },
    )
    await redis.xgroup_create(f"worker:{worker_id}:input", "worker_group", id="0", mkstream=True)


@pytest.mark.asyncio
async def test_a_developer_worker_from_before_the_cutover_keeps_working_across_the_rollout(monkeypatch):
    """The rollout event: new broker, worker containers that outlived it.

    A developer worker's container and its Redis record are not Compose
    services, so replacing the control plane leaves them running. Its
    credential predates the recorded type, and every route now decides from
    that type — so without the startup migration this worker loses its lease,
    its status, its session and the channel it reports its result on, in the
    middle of real product work.
    """
    redis = FakeAsyncRedis(decode_responses=True)
    main.app.state.redis = redis
    legacy_token = "l" * 43
    qa_token = "q" * 43
    await _pre_cutover_credential(redis, "legacy-dev", legacy_token)
    await main.register_worker(
        main.Registration(
            worker_id="qa-executor",
            token=qa_token,
            worker_type=WorkerType.QA,
            input_stream="worker:qa-executor:input",
            output_stream="worker:qa-executor:output",
        ),
        main.settings.BROKER_INTERNAL_TOKEN,
    )

    # Before the migration the strict policy refuses it — this is what a rollout
    # without one does to a live worker.
    with pytest.raises(main.HTTPException) as denied:
        await main.lease_input("legacy-dev", legacy_token)
    assert denied.value.status_code == 403

    assert await main.migrate_pre_cutover_credentials(redis) == 1

    # The whole turn protocol, over the real routes, with the credential the
    # running wrapper already holds.
    await redis.xadd("worker:legacy-dev:input", {"data": json.dumps({"task_id": "task-9", "prompt": "keep going"})})
    lease = await main.lease_input("legacy-dev", legacy_token)
    assert lease["data"]["task_id"] == "task-9"
    await main.update_status("legacy-dev", main.StatusUpdate(values={"status": "running"}), legacy_token)
    await main.set_session("legacy-dev", main.SessionUpdate(session_id="resumed"), legacy_token)
    assert (await main.get_session("legacy-dev", legacy_token))["session_id"] == "resumed"
    await main.submit_output(
        "legacy-dev",
        main.Submission(lease_id=lease["lease_id"], result={"status": "failed", "error": "finished after rollout"}),
        legacy_token,
    )
    assert json.loads((await redis.xrange("worker:legacy-dev:output"))[0][1]["data"])["status"] == "failed"

    # And it is a developer worker in full, including the route a developer
    # needs to verify its own change.
    forwarded = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        forwarded["url"] = str(request.url)
        return httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": ""})

    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(upstream)
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: real_async_client(transport=transport, **kwargs))
    assert (await main.compose("legacy-dev", {"args": ["ps"]}, legacy_token)).status_code == 200
    assert forwarded["url"].endswith("/api/worker/legacy-dev/infra/compose")

    # The migration touched nothing else: the QA executor keeps the type the
    # server recorded for it, and keeps being refused the daemon.
    assert await redis.hget(credential_key("qa-executor"), "worker_type") == WorkerType.QA.value
    with pytest.raises(main.HTTPException) as qa_denied:
        await main.compose("qa-executor", {"args": ["build"]}, qa_token)
    assert qa_denied.value.detail == "a qa worker may not call infra.compose"


@pytest.mark.asyncio
async def test_the_migration_is_a_cutover_and_not_a_standing_fallback():
    """A typeless record that appears after the migration is still refused.

    The migration runs once, at startup, over the records that existed then.
    Nothing at request time consults it, so a credential written afterwards
    without a type — which registration cannot produce — is refused every
    route, as `test_a_credential_registered_without_a_recorded_type_is_refused_everything`
    asserts. Running the migration a second time is what a restart does, and it
    must not resurrect such a record without a second startup.
    """
    redis = FakeAsyncRedis(decode_responses=True)
    main.app.state.redis = redis
    token = "s" * 43
    assert await main.migrate_pre_cutover_credentials(redis) == 0

    await _pre_cutover_credential(redis, "appeared-later", token)
    with pytest.raises(main.HTTPException) as denied:
        await main.lease_input("appeared-later", token)
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
