"""When a QA run's use of the shared Telegram identity has provably ended.

The run's hold on the QA account owns an account of every lifetime that can use
the session: the identity proof's client, each probe child, the capability
endpoint and each executor sandbox. Each is registered before the await that could
open it and ends only on positive proof. These cases drive the real runner, the
real capability endpoint (on loopback), the real executor client and the real
lease (Lua on an in-memory Redis) together, with worker-manager's answers and the
Redis transport under the test's control, and inject a fault at each step.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
from fakeredis.aioredis import FakeRedis
import pytest

from shared.contracts.bot_access import QA_TEST_TELEGRAM_ID
from shared.contracts.queues.worker import CreateWorkerCommand, WorkerOwnership
from shared.contracts.vocab import AgentType
from shared.qa_probe_cli import TELEGRAM_IDENTITY_CALL
from shared.telegram_access_probe import run_probe_script
from src.agents.qa.capability_service import QACapabilityService
from src.consumers import qa
from src.consumers._qa_runner import QARuntimeConfig, run_qa_centrally
from src.consumers._qa_telegram_identity import prove_sandbox_telegram_identity
from src.consumers._qa_telegram_lease import (
    Holder,
    HolderKind,
    IdentityBusy,
    IdentityHold,
    TelegramIdentityLease,
)
from tests.unit.test_qa_central_runtime import TARGET, FakeConn

TELETHON = {
    "TELETHON_API_ID": "12345",
    "TELETHON_API_HASH": "0123456789abcdef0123456789abcdef",
    "TELETHON_SESSION": "1BQANOTEuMTA4LjU2LjE-release-session",
}
QA = Holder(HolderKind.NATIVE_QA, "qa-run-1", "exploratory")
BUYER = Holder(HolderKind.SYNTHETIC_BUYER, "s1487-buyer-001", "buyer:probe")
VERDICT = '{"pass": true, "checks": [], "summary": "OK"}'
REAL_STOP = QACapabilityService.stop


async def _idle_lease() -> TelegramIdentityLease:
    lease = TelegramIdentityLease(FakeRedis(), QA_TEST_TELEGRAM_ID)
    assert await lease.initialize()
    return lease


class WorkerManagerSide:
    """The Redis transport of one executor, and worker-manager's answers on it.

    The sandbox is driven through the create command it was really given: it posts
    to the run's capability endpoint for the QA session, then submits a verdict.
    """

    def __init__(self, delete_answer, *, delete_publish_fails: bool = False) -> None:
        self.delete_answer = delete_answer
        self.delete_publish_fails = delete_publish_fails
        self.created: CreateWorkerCommand | None = None
        self.deletes = 0
        self.served_identity: dict | None = None
        self.client = AsyncMock()
        self.client.xadd.side_effect = self._xadd

    async def _xadd(self, _stream, fields, **_kwargs):
        if self.created is None:
            self.created = CreateWorkerCommand.model_validate_json(fields["data"])
            return "1-0"
        self.deletes += 1
        if self.delete_publish_fails:
            raise ConnectionError("redis went away")
        return "2-0"

    async def respond(self, _client, _group, _consumer, request_id, *_args, **_kwargs):
        if not request_id.startswith("cleanup-"):
            return {"success": True}
        if self.delete_answer == "never":
            await asyncio.Event().wait()
        return self.delete_answer

    async def sandbox(self, **_kwargs):
        env = self.created.config.env_vars
        headers = {"Authorization": f"Bearer {env['QA_CAPABILITY_TOKEN']}"}
        async with aiohttp.ClientSession() as session:
            for call in (
                {"tool": TELEGRAM_IDENTITY_CALL, "args": {}},
                {"tool": "submit_qa_result", "args": {"result": VERDICT}},
            ):
                async with session.post(
                    env["QA_CAPABILITY_URL"], json=call, headers=headers
                ) as response:
                    answer = await response.json()
                    if call["tool"] == TELEGRAM_IDENTITY_CALL:
                        self.served_identity = answer
        return "", None


async def _run_under_hold(lease, side: WorkerManagerSide, tmp_path, *, stop=None):
    async with lease.hold(QA, wait_seconds=0, poll_seconds=1) as held:
        runtime = QARuntimeConfig(
            executor_agent_type=AgentType.CODEX,
            capability_host="127.0.0.1",
            telethon_env=TELETHON,
            telegram_identity_proven=True,
            telegram_hold=held,
        )
        patches = [
            patch("src.consumers._qa_target._connect", AsyncMock(return_value=FakeConn())),
            patch("src.consumers._qa_target._import", lambda key: key),
            patch("src.consumers._qa_workspace.QA_WORKSPACE_ROOT", str(tmp_path / "qa-runs")),
            patch("src.clients.qa_worker.redis.from_url", return_value=side.client),
            patch("src.clients.qa_worker.get_settings", return_value=SimpleNamespace(redis_url="")),
            patch("src.clients.qa_worker._wait_for_response", side_effect=side.respond),
            patch("src.clients.qa_worker._wait_until_ready", AsyncMock(return_value=None)),
            patch("src.clients.qa_worker.ensure_worker_output_group", AsyncMock(return_value="o")),
            patch("src.clients.qa_worker.publish_worker_turn", AsyncMock()),
            patch("src.clients.qa_worker._await_verdict_or_exit", side_effect=side.sandbox),
        ]
        if stop is not None:
            patches.append(patch.object(QACapabilityService, "stop", stop))
        for each in patches:
            each.start()
        try:
            return held, await run_qa_centrally(
                target=TARGET,
                ownership=WorkerOwnership(
                    story_id="story-1",
                    project_id="proj-qa",
                    run_id="qa-run-1",
                    attempt_id="attempt-qa-run-1",
                ),
                fleet_ssh_key="fleet-key",
                acceptance_criteria="- GET /health returns 200",
                runtime=runtime,
                grant_journal=SimpleNamespace(write=AsyncMock()),
                provisioning_journal=SimpleNamespace(missing_identity=AsyncMock()),
                established_facts=[],
            )
        finally:
            for each in patches:
                each.stop()


async def _second_applicant_refused(lease) -> str:
    second = TelegramIdentityLease(lease._redis, QA_TEST_TELEGRAM_ID)  # noqa: SLF001
    with pytest.raises(IdentityBusy) as busy:
        async with second.hold(BUYER, wait_seconds=0, poll_seconds=1):
            pytest.fail("a second user was admitted past an outstanding lifetime")
    return str(busy.value)


async def test_a_sandbox_served_the_session_and_proven_removed_releases_the_identity(tmp_path):
    lease = await _idle_lease()
    side = WorkerManagerSide({"success": True})

    held, result = await _run_under_hold(lease, side, tmp_path)

    assert side.served_identity["session"] == TELETHON["TELETHON_SESSION"]
    assert result.passed
    assert [used.kind for used in held.lifetimes] == ["endpoint", "sandbox"]
    assert all(used.ended for used in held.lifetimes)
    assert (await lease.holder())["state"] == "idle"


@pytest.mark.parametrize(
    ("answer", "named"),
    [
        (
            {"success": False, "error": "QA executor removal not proven: executor container"},
            "did not prove the removal: QA executor removal not proven: executor container",
        ),
        (None, "did not answer the delete"),
    ],
)
async def test_a_removal_worker_manager_did_not_prove_retains_the_identity(tmp_path, answer, named):
    lease = await _idle_lease()
    side = WorkerManagerSide(answer)

    await _run_under_hold(lease, side, tmp_path)

    record = await lease.holder()
    assert record["state"] == "retained"
    assert "sandbox qa-" in record["retained"]
    assert named in record["retained"]
    assert "retained because outstanding: sandbox qa-" in await _second_applicant_refused(lease)


async def test_a_delete_that_could_not_be_published_retains_the_identity(tmp_path):
    lease = await _idle_lease()
    side = WorkerManagerSide({"success": True}, delete_publish_fails=True)

    # The runner settles the failure as it settles any; the hold is what matters here.
    with suppress(ConnectionError):
        await _run_under_hold(lease, side, tmp_path)

    record = await lease.holder()
    assert record["state"] == "retained"
    assert "the delete could not be published: ConnectionError" in record["retained"]
    await _second_applicant_refused(lease)


async def test_a_run_cancelled_while_awaiting_the_removal_answer_retains_the_identity(tmp_path):
    lease = await _idle_lease()
    side = WorkerManagerSide("never")

    run = asyncio.create_task(_run_under_hold(lease, side, tmp_path))
    for _ in range(500):
        await asyncio.sleep(0)
        if side.deletes:
            break
    await asyncio.sleep(0)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    assert side.deletes == 1
    record = await lease.holder()
    assert record["state"] == "retained"
    assert "sandbox qa-" in record["retained"]
    await _second_applicant_refused(lease)


async def test_an_endpoint_that_did_not_stop_retains_the_identity(tmp_path):
    lease = await _idle_lease()
    side = WorkerManagerSide({"success": True})

    stopped = []

    async def stop_fails(service):
        stopped.append(service)
        raise RuntimeError("runner cleanup failed")

    with suppress(RuntimeError):
        await _run_under_hold(lease, side, tmp_path, stop=stop_fails)

    record = await lease.holder()
    assert record["state"] == "retained"
    assert (
        "endpoint capability endpoint (the endpoint did not stop: RuntimeError)"
        in (record["retained"])
    )
    await _second_applicant_refused(lease)


async def test_a_run_cancelled_while_its_endpoint_stops_retains_the_identity(tmp_path):
    lease = await _idle_lease()
    side = WorkerManagerSide({"success": True})
    stopping = asyncio.Event()

    stopped = []

    async def stop_hangs(service):
        stopped.append(service)
        stopping.set()
        await asyncio.Event().wait()

    run = asyncio.create_task(_run_under_hold(lease, side, tmp_path, stop=stop_hangs))
    await stopping.wait()
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    record = await lease.holder()
    assert record["state"] == "retained"
    assert "endpoint capability endpoint" in record["retained"]
    await _second_applicant_refused(lease)
    await REAL_STOP(stopped[0])


# --- the identity proof's and the probes' clients ------------------------------------


class ProofClient:
    def __init__(self, *, authorize=None, disconnect=None) -> None:
        self._authorize = authorize
        self._disconnect = disconnect
        self.connected = False

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        if self._authorize is not None:
            return await self._authorize()
        return True

    async def get_me(self):
        return SimpleNamespace(id=QA_TEST_TELEGRAM_ID)

    async def disconnect(self):
        if self._disconnect is not None:
            await self._disconnect()
        self.connected = False


def _hold() -> IdentityHold:
    return IdentityHold(QA, token="t")  # noqa: S106 - a hold token, not a password


def _runtime(hold: IdentityHold) -> QARuntimeConfig:
    return QARuntimeConfig(
        executor_agent_type=AgentType.CODEX,
        capability_host="qa",
        telethon_env=TELETHON,
        telegram_hold=hold,
    )


async def test_an_authorization_failure_after_connect_still_ends_the_proof_by_disconnect():
    hold = _hold()

    async def fails():
        raise ConnectionError("dropped")

    client = ProofClient(authorize=fails)
    proven = await prove_sandbox_telegram_identity(
        _runtime(hold), client_factory=lambda _env: client
    )

    assert not proven.telegram_identity_proven
    assert not client.connected
    assert hold.outstanding() == []


async def test_an_identity_proof_whose_disconnect_fails_stays_outstanding():
    hold = _hold()

    async def stuck():
        raise ConnectionError("stuck")

    await prove_sandbox_telegram_identity(
        _runtime(hold), client_factory=lambda _env: ProofClient(disconnect=stuck)
    )

    (used,) = hold.outstanding()
    assert used.describe() == "client identity proof (its disconnect failed: ConnectionError)"


async def test_an_identity_proof_cancelled_at_its_disconnect_stays_outstanding():
    hold = _hold()
    reached = asyncio.Event()

    async def hang():
        reached.set()
        await asyncio.Event().wait()

    client = ProofClient(disconnect=hang)
    proof = asyncio.create_task(
        prove_sandbox_telegram_identity(_runtime(hold), client_factory=lambda _env: client)
    )
    await reached.wait()
    proof.cancel()
    with pytest.raises(asyncio.CancelledError):
        await proof

    (used,) = hold.outstanding()
    assert used.name == "identity proof"


class Child:
    """A probe child process under the test's control."""

    def __init__(self, *, exits_on_kill: bool = True) -> None:
        self.returncode = None
        self.killed = False
        self._exits_on_kill = exits_on_kill
        self._exited = asyncio.Event()

    async def communicate(self):
        await asyncio.Event().wait()

    async def wait(self):
        await self._exited.wait()
        return self.returncode

    def kill(self):
        self.killed = True
        if self._exits_on_kill:
            self.returncode = -9
            self._exited.set()


@pytest.mark.parametrize(("exits", "outstanding"), [(True, False), (False, True)])
async def test_a_cancelled_probe_child_ends_only_once_its_exit_is_seen(exits, outstanding):
    hold = _hold()
    child = Child(exits_on_kill=exits)
    used = hold.track("probe", "bot access preflight")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=child)):
        probe = asyncio.create_task(
            run_probe_script("print(1)", env=TELETHON, timeout=60, lifetime=used)
        )
        for _ in range(3):
            await asyncio.sleep(0)
        probe.cancel()
        if not exits:
            for _ in range(3):
                await asyncio.sleep(0)
            probe.cancel()  # the wait for its exit is cancelled too
        with pytest.raises(asyncio.CancelledError):
            await probe

    assert child.killed
    assert (used in hold.outstanding()) is outstanding


async def test_a_mechanical_probe_never_opens_a_session_beside_another_holder(monkeypatch):
    from shared.contracts.dto.temporary_access import TemporaryAccessStatus
    from shared.contracts.queues.qa import QAMessage
    from src.consumers import mechanical_telegram
    from src.consumers._qa_redaction import QARunRedaction
    from src.consumers._qa_runner import QAResult

    msg = QAMessage(
        project_id="project",
        story_id="story",
        initiating_run_id="stand-run",
        run_id="qa-run",
        application_id=42,
        deployed_url="https://product.example",
        bot_username="stand_bot",
        acceptance_criteria="- GET /health returns 200\n- Stand mechanical notes: unique",
    )
    grant = SimpleNamespace(
        id="tempaccess-qa-run",
        status=TemporaryAccessStatus.GRANTED,
        qa_run_id=msg.run_id,
        project_id=msg.project_id,
        target_application_id=msg.application_id,
        target_base_url=msg.deployed_url,
        channel="telegram",
        external_id=str(QA_TEST_TELEGRAM_ID),
        qa_message=msg,
        head_sha="a" * 40,
        granted_at=datetime.now(UTC),
    )
    seconds = [0.0]

    async def sleep(delay):
        seconds[0] += delay

    redis = FakeRedis()
    lease = TelegramIdentityLease(redis, QA_TEST_TELEGRAM_ID, wall=lambda: seconds[0], sleep=sleep)
    assert await lease.initialize()
    await lease._acquire(BUYER, 0, 0)  # noqa: SLF001 - the buyer's admission
    probe = AsyncMock()
    monkeypatch.setattr(
        qa,
        "api_client",
        SimpleNamespace(
            get_temporary_access_grant=AsyncMock(return_value=grant), patch=AsyncMock()
        ),
    )
    monkeypatch.setattr(mechanical_telegram, "run_fixed_probe", probe)

    with pytest.raises(IdentityBusy):
        await qa._run_mechanical_qa(
            msg,
            ("notes", "unique"),
            {"USER_IDENTITY_CAPABILITY": "cap"},
            QAResult(passed=True),
            QARunRedaction(),
            lease,
        )

    probe.assert_not_awaited()
    assert seconds[0] >= qa.QA_TELEGRAM_IDENTITY_WAIT_SECONDS
