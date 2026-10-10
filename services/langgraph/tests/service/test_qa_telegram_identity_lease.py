"""The shared QA Telegram identity's hold, against a real Redis.

The unit suite drives the same adapter over an in-memory Redis. Here the Lua
scripts and the state record run against the service leg's Redis: admission is
atomic under contention and only from a known `idle`; a lost record admits no one
while its first holder is still in use; nothing expires by time; a holder whose
record is taken stops its use.

The last cases run a native QA run end to end over that Redis: the real runner,
the real capability endpoint serving the QA session to the sandbox, the real
executor client publishing create and delete commands on the real
`worker:commands` stream and reading worker-manager's answers off the real
`worker:responses` stream. Worker-manager itself is the one stand-in, answering on
those streams in its wire shape; the truth of its answer is proven against real
Docker in the worker-manager service leg (`test_qa_removal_answer.py`).
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

import aiohttp
import pytest

os.environ.setdefault("API_BASE_URL", "http://localhost:8001")
os.environ.setdefault("INTERNAL_API_KEY", "test-key")

from shared.contracts.dto.worker import WorkerStatus
from shared.contracts.queues.worker import (
    CreateWorkerCommand,
    CreateWorkerResponse,
    DeleteWorkerCommand,
    DeleteWorkerResponse,
    WorkerOwnership,
)
from shared.contracts.vocab import AgentType
from shared.qa_probe_cli import TELEGRAM_IDENTITY_CALL
from shared.queues import WORKER_COMMANDS, WORKER_RESPONSES
from src.consumers._qa_runner import QARuntimeConfig, run_qa_centrally
from src.consumers._qa_telegram_lease import (
    Holder,
    HolderKind,
    IdentityBusy,
    IdentityOwnershipLost,
    TelegramIdentityLease,
)
from tests.unit.test_qa_central_runtime import TARGET, FakeConn

TELETHON = {
    "TELETHON_API_ID": "12345",
    "TELETHON_API_HASH": "0123456789abcdef0123456789abcdef",
    "TELETHON_SESSION": "1BQANOTEuMTA4LjU2LjE-service-session",
}


@pytest.fixture
async def account(real_redis):
    """A Telegram id of this test alone, so no other case shares its lease key."""
    telegram_id = int(uuid.uuid4().int % 10**9) + 10**9
    yield telegram_id
    await real_redis.delete(f"qa:telegram-identity:{telegram_id}")


@pytest.fixture
async def idle(real_redis, account):
    assert await TelegramIdentityLease(real_redis, account).initialize()
    return account


def _holder(index: int) -> Holder:
    kind = HolderKind.NATIVE_QA if index % 2 else HolderKind.SYNTHETIC_BUYER
    return Holder(kind, f"holder-{index}", "contention")


async def test_contending_holders_are_admitted_one_at_a_time(real_redis, idle):
    inside = 0
    most = 0
    admitted = []

    async def use(index: int) -> None:
        nonlocal inside, most
        lease = TelegramIdentityLease(real_redis, idle)
        async with lease.hold(_holder(index), wait_seconds=30, poll_seconds=0.01) as held:
            client = held.track("client", f"holder-{index}")
            inside += 1
            most = max(most, inside)
            admitted.append(index)
            await asyncio.sleep(0.02)
            inside -= 1
            client.end()

    await asyncio.gather(*(use(index) for index in range(8)))

    assert most == 1
    assert sorted(admitted) == list(range(8))
    assert await real_redis.hget(f"qa:telegram-identity:{idle}", "state") == b"idle"


async def test_a_missing_record_admits_no_one_and_acquire_never_creates_it(real_redis, account):
    lease = TelegramIdentityLease(real_redis, account)

    with pytest.raises(IdentityBusy, match="missing or unknown"):
        async with lease.hold(_holder(1), wait_seconds=0.2, poll_seconds=0.05):
            pytest.fail("admitted with no known admission state")

    assert await real_redis.exists(lease.key) == 0


async def test_a_lost_record_admits_no_second_user_beside_the_first(real_redis, idle):
    """The record is lost while A uses the session; B applies before A's watchdog."""
    first = TelegramIdentityLease(real_redis, idle, renew_wait=lambda: asyncio.sleep(3600))
    in_use = asyncio.Event()
    release = asyncio.Event()

    async def holder_a():
        async with first.hold(_holder(1), wait_seconds=1, poll_seconds=0.05) as held:
            client = held.track("client", "A")
            in_use.set()
            await release.wait()
            client.end()

    task = asyncio.create_task(holder_a())
    await in_use.wait()
    await real_redis.delete(first.key)

    with pytest.raises(IdentityBusy, match="missing or unknown"):
        async with TelegramIdentityLease(real_redis, idle).hold(
            _holder(2), wait_seconds=0.3, poll_seconds=0.05
        ):
            pytest.fail("a second user was admitted while the first was in use")

    release.set()
    with pytest.raises(IdentityOwnershipLost):
        await task
    assert await real_redis.exists(first.key) == 0


async def test_an_outstanding_use_retains_without_ttl_and_yields_only_to_its_token(
    real_redis, idle
):
    lease = TelegramIdentityLease(real_redis, idle)
    async with lease.hold(_holder(1), wait_seconds=1, poll_seconds=0.05) as held:
        held.track("sandbox", "qa-1").unproven("worker-manager did not prove the removal")

    assert await real_redis.ttl(lease.key) == -1
    assert await real_redis.hget(lease.key, "state") == b"retained"
    with pytest.raises(IdentityBusy, match="retained because outstanding: sandbox qa-1"):
        async with lease.hold(_holder(2), wait_seconds=0.3, poll_seconds=0.05):
            pytest.fail("admitted beside a retained hold")
    assert not await lease.release("another-token")
    assert await lease.release(held.token)
    async with lease.hold(_holder(2), wait_seconds=1, poll_seconds=0.05):
        pass


async def test_a_holder_whose_record_is_taken_stops_its_use(real_redis, idle):
    lease = TelegramIdentityLease(real_redis, idle, renew_wait=lambda: asyncio.sleep(0.05))
    stopped = asyncio.Event()
    in_use = asyncio.Event()

    async def use() -> None:
        async with lease.hold(_holder(1), wait_seconds=1, poll_seconds=0.05):
            try:
                in_use.set()
                await asyncio.sleep(30)
            finally:
                stopped.set()

    task = asyncio.create_task(use())
    await in_use.wait()
    await real_redis.delete(lease.key)

    with pytest.raises(IdentityOwnershipLost):
        await asyncio.wait_for(task, timeout=5)
    assert stopped.is_set()


# --- a native QA run over the real streams ---------------------------------------------


class WorkerManagerOnStreams:
    """Worker-manager's side of the real streams, answering in its wire shape.

    On create it acknowledges and marks the executor running; the sandbox then asks
    the run's capability endpoint for the QA session and submits a verdict. On
    delete it answers with `delete_success`, or never (`None`).
    """

    def __init__(self, redis, delete_success: bool | None) -> None:
        self.redis = redis
        self.delete_success = delete_success
        self.group = f"wm-{uuid.uuid4().hex[:8]}"
        self.served: dict | None = None
        self.deletes = 0

    async def start(self) -> None:
        await self.redis.xgroup_create(WORKER_COMMANDS, self.group, id="$", mkstream=True)
        self.task = asyncio.create_task(self._serve())

    async def stop(self) -> None:
        self.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await self.task
        await self.redis.xgroup_destroy(WORKER_COMMANDS, self.group)

    async def _answer(self, response) -> None:
        await self.redis.xadd(WORKER_RESPONSES, {"data": response.model_dump_json()})

    async def _serve(self) -> None:
        while True:
            read = await self.redis.xreadgroup(
                self.group, "wm", {WORKER_COMMANDS: ">"}, count=1, block=100
            )
            for _, entries in read or []:
                for _, fields in entries:
                    command = json.loads(fields[b"data"])
                    if command.get("command") == "create":
                        create = CreateWorkerCommand.model_validate(command)
                        await self._answer(
                            CreateWorkerResponse(
                                request_id=create.request_id,
                                success=True,
                                worker_id=create.config.name,
                            )
                        )
                        await self.redis.hset(
                            f"worker:status:{create.config.name}",
                            mapping={"status": WorkerStatus.RUNNING.value},
                        )
                        asyncio.create_task(self._sandbox(create))
                    elif command.get("command") == "delete":
                        delete = DeleteWorkerCommand.model_validate(command)
                        self.deletes += 1
                        if self.delete_success is not None:
                            await self._answer(
                                DeleteWorkerResponse(
                                    request_id=delete.request_id,
                                    success=self.delete_success,
                                    error=None
                                    if self.delete_success
                                    else "QA executor removal not proven: executor container",
                                )
                            )

    async def _sandbox(self, create: CreateWorkerCommand) -> None:
        env = create.config.env_vars
        headers = {"Authorization": f"Bearer {env['QA_CAPABILITY_TOKEN']}"}
        async with aiohttp.ClientSession() as session:
            for call in (
                {"tool": TELEGRAM_IDENTITY_CALL, "args": {}},
                {
                    "tool": "submit_qa_result",
                    "args": {"result": '{"pass": true, "checks": [], "summary": "OK"}'},
                },
            ):
                async with session.post(env["QA_CAPABILITY_URL"], json=call, headers=headers) as r:
                    answer = await r.json()
                    if call["tool"] == TELEGRAM_IDENTITY_CALL:
                        self.served = answer


async def _native_run(real_redis, account, side, tmp_path):
    lease = TelegramIdentityLease(real_redis, account)
    async with lease.hold(_holder(1), wait_seconds=1, poll_seconds=0.05) as held:
        runtime = QARuntimeConfig(
            executor_agent_type=AgentType.CODEX,
            capability_host="127.0.0.1",
            telethon_env=TELETHON,
            telegram_identity_proven=True,
            telegram_hold=held,
        )
        with (
            patch("src.consumers._qa_target._connect", AsyncMock(return_value=FakeConn())),
            patch("src.consumers._qa_target._import", lambda key: key),
            patch("src.consumers._qa_workspace.QA_WORKSPACE_ROOT", str(tmp_path / "qa-runs")),
            patch(
                "src.clients.qa_worker.get_settings",
                return_value=SimpleNamespace(
                    redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0")
                ),
            ),
            patch("src.clients.qa_worker.VERDICT_GRACE_S", 0.2),
            patch("src.clients.qa_worker.REMOVAL_CONFIRMATION_S", 2),
        ):
            await run_qa_centrally(
                target=TARGET,
                ownership=WorkerOwnership(
                    story_id="story-1", project_id="proj-qa", run_id="qa-1", attempt_id="qa-1"
                ),
                fleet_ssh_key="fleet-key",
                acceptance_criteria="- GET /health returns 200",
                runtime=runtime,
                grant_journal=SimpleNamespace(write=AsyncMock()),
                provisioning_journal=SimpleNamespace(missing_identity=AsyncMock()),
                established_facts=[],
                timeout=10,
            )
    return held


@pytest.mark.parametrize(
    ("delete_success", "state"), [(True, b"idle"), (False, b"retained"), (None, b"retained")]
)
async def test_a_native_run_releases_only_on_a_proven_sandbox_removal(
    real_redis, idle, tmp_path, delete_success, state
):
    side = WorkerManagerOnStreams(real_redis, delete_success)
    await side.start()
    try:
        held = await _native_run(real_redis, idle, side, tmp_path)
    finally:
        await side.stop()

    assert [used.kind for used in held.lifetimes] == ["endpoint", "sandbox"]
    assert side.served["session"] == TELETHON["TELETHON_SESSION"]
    assert side.deletes == 1
    assert await real_redis.hget(held_key(idle), "state") == state
    if state == b"retained":
        retained = (await real_redis.hget(held_key(idle), "retained")).decode()
        assert retained.startswith("outstanding: sandbox qa-")
        with pytest.raises(IdentityBusy):
            async with TelegramIdentityLease(real_redis, idle).hold(
                _holder(2), wait_seconds=0.2, poll_seconds=0.05
            ):
                pytest.fail("a second user was admitted past an unproven removal")


async def test_a_native_run_cancelled_at_the_removal_answer_retains(real_redis, idle, tmp_path):
    side = WorkerManagerOnStreams(real_redis, None)
    await side.start()
    try:
        run = asyncio.create_task(_native_run(real_redis, idle, side, tmp_path))
        for _ in range(200):
            await asyncio.sleep(0.05)
            if side.deletes:
                break
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
    finally:
        await side.stop()

    assert side.deletes == 1
    assert await real_redis.hget(held_key(idle), "state") == b"retained"


def held_key(account: int) -> str:
    return f"qa:telegram-identity:{account}"
