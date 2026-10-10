"""When a QA run's use of the shared Telegram identity has provably ended.

A run's hold on the QA account is released only after every user of the session it
started has stopped: the identity proof's client disconnected, the mechanical
probe's client disconnected, a probe child process killed with its caller, and every
executor sandbox that was served the session confirmed removed by worker-manager.
Anything short of that retains the hold.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fakeredis.aioredis import FakeRedis
import pytest

from shared.contracts.bot_access import QA_TEST_TELEGRAM_ID
from shared.contracts.queues.worker import QA_TARGET_REFUSED, WorkerOwnership
from shared.contracts.vocab import AgentType
from shared.telegram_access_probe import run_probe_script
from src.clients.qa_worker import ExecutorRemovals, QAExecutorUnavailable, run_qa_executor
from src.consumers import qa
from src.consumers._qa_runner import QARuntimeConfig, _account_for_served_identity
from src.consumers._qa_telegram_identity import prove_sandbox_telegram_identity
from src.consumers._qa_telegram_lease import (
    Holder,
    HolderKind,
    IdentityBusy,
    IdentityHold,
    TelegramIdentityLease,
)

TELETHON = {
    "TELETHON_API_ID": "12345",
    "TELETHON_API_HASH": "0123456789abcdef0123456789abcdef",
    "TELETHON_SESSION": "1BQANOTEuMTA4LjU2LjE-release-session",
}


def _hold() -> IdentityHold:
    return IdentityHold(Holder(HolderKind.NATIVE_QA, "qa-1", "exploratory"), token="t")  # noqa: S106 - a hold token, not a password


async def _executor_with_delete_answer(answer, removals):
    """One executor that fails after creation, so its container is deleted in `finally`."""
    redis_client = AsyncMock()

    async def respond(_client, _group, _consumer, request_id, *_args, **_kwargs):
        if request_id.startswith("cleanup-"):
            if isinstance(answer, Exception):
                raise answer
            return answer
        return {"success": True}

    with (
        patch("src.clients.qa_worker.redis.from_url", return_value=redis_client),
        patch("src.clients.qa_worker._wait_for_response", side_effect=respond) as waits,
        patch(
            "src.clients.qa_worker._wait_until_ready",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(output=f"{QA_TARGET_REFUSED}: refused"),
        ),
        pytest.raises(QAExecutorUnavailable),
    ):
        await run_qa_executor(
            agent_type=AgentType.CODEX,
            ownership=WorkerOwnership(
                story_id="story-1", project_id="project-1", run_id="qa-1", attempt_id="qa-1"
            ),
            deploy_target_url="https://product.example",
            capability_url="http://qa-worker:41234/qa/call",
            capability_token="run-token",  # noqa: S106 - fake endpoint credential
            instructions="# QA executor",
            prompt="test it",
            verdict_received=asyncio.Event(),
            calls_served=lambda: 0,
            timeout=1,
            on_create_published=lambda: None,
            **({} if removals is None else {"removals": removals}),
        )
    return [call.args[3] for call in waits.call_args_list]


@pytest.mark.parametrize(
    ("answer", "confirmed", "unconfirmed"),
    [
        ({"success": True}, ["qa-"], ""),
        (None, [], "did not answer the delete"),
        ({"success": False, "error": "container busy"}, [], "refused the delete: container busy"),
        (RuntimeError("redis gone"), [], "could not be read: RuntimeError"),
    ],
)
async def test_an_executor_removal_counts_only_when_worker_manager_confirms_it(
    answer, confirmed, unconfirmed
):
    removals = ExecutorRemovals()

    await _executor_with_delete_answer(answer, removals)

    assert [worker[:3] for worker in removals.confirmed] == confirmed
    if unconfirmed:
        (reason,) = removals.unconfirmed.values()
        assert unconfirmed in reason
    else:
        assert removals.unconfirmed == {}


async def test_a_run_without_the_identity_does_not_wait_for_the_delete_answer():
    requests = await _executor_with_delete_answer({"success": True}, None)

    assert not any(request.startswith("cleanup-") for request in requests)


@pytest.mark.parametrize(
    ("served", "unconfirmed", "retained"),
    [(0, {"qa-1": "no answer"}, False), (1, {}, False), (1, {"qa-1": "no answer"}, True)],
)
def test_only_a_served_sandbox_whose_removal_is_unconfirmed_retains_the_hold(
    served, unconfirmed, retained
):
    hold = _hold()
    runtime = QARuntimeConfig(
        executor_agent_type=AgentType.CODEX, capability_host="qa", telegram_hold=hold
    )

    _account_for_served_identity(
        runtime,
        SimpleNamespace(identity_served=served),
        ExecutorRemovals(unconfirmed=dict(unconfirmed)),
    )

    assert (hold.retained is not None) is retained
    if retained:
        assert "qa-1: no answer" in hold.retained


async def test_an_identity_proof_whose_disconnect_fails_retains_the_hold():
    hold = _hold()

    class Client:
        async def connect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return SimpleNamespace(id=QA_TEST_TELEGRAM_ID)

        async def disconnect(self):
            raise ConnectionError("stuck")

    runtime = QARuntimeConfig(
        executor_agent_type=AgentType.CODEX,
        capability_host="qa",
        telethon_env=TELETHON,
        telegram_hold=hold,
    )

    proven = await prove_sandbox_telegram_identity(runtime, client_factory=lambda _env: Client())

    assert proven.telegram_identity_proven
    assert hold.retained == "the identity proof's disconnect failed: ConnectionError"


async def test_a_cancelled_probe_kills_the_child_that_holds_the_session():
    process = SimpleNamespace(returncode=None, killed=False)

    async def communicate():
        await asyncio.Event().wait()

    async def wait():
        return -9

    def kill():
        process.killed = True
        process.returncode = -9

    process.communicate, process.wait, process.kill = communicate, wait, kill
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
        probe = asyncio.create_task(run_probe_script("print(1)", env=TELETHON, timeout=60))
        for _ in range(3):
            await asyncio.sleep(0)
        probe.cancel()
        with pytest.raises(asyncio.CancelledError):
            await probe

    assert process.killed


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

    lease = TelegramIdentityLease(
        FakeRedis(), QA_TEST_TELEGRAM_ID, wall=lambda: seconds[0], sleep=sleep
    )
    await lease._acquire(  # noqa: SLF001 - the buyer's admission
        Holder(HolderKind.SYNTHETIC_BUYER, "s1487-buyer-001", "buyer:probe"), 0, 0
    )
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
