"""The QA sandbox's Telegram identity and deploy target, as the QA runtime hands them over.

The sandbox may run its own Telethon client as the QA account, so the runtime
proves the session for every run before the capability endpoint will serve it,
and a session that fails the proof is treated exactly as missing credentials.
The value itself travels only as the answer to the run token, lands in a private
file in the container, and is scrubbed out of anything the sandbox says back.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from shared.contracts.bot_access import QA_TEST_TELEGRAM_ID
from shared.contracts.dto.run_result import QABlockerCategory
from shared.contracts.queues.worker import CreateWorkerCommand, WorkerOwnership
from shared.contracts.vocab import AgentType
from shared.qa_probe_cli import QA_PROBE_SCRIPT, TELEGRAM_IDENTITY_CALL
from src.agents.qa.capability_service import QACapabilityService
from src.clients.qa_worker import run_qa_executor
from src.consumers._qa_runner import QARuntimeConfig, preflight_bot_access
from src.consumers._qa_telegram_identity import (
    REDACTED,
    handed_over_secrets,
    identity_record,
    prove_sandbox_telegram_identity,
    redact,
)

SESSION = "1BQANOTEuMTA4LjU2LjE-sandbox-session-value"
API_HASH = "0123456789abcdef0123456789abcdef"
TELETHON = {
    "TELETHON_API_ID": "12345",
    "TELETHON_API_HASH": API_HASH,
    "TELETHON_SESSION": SESSION,
}
RUNTIME = QARuntimeConfig(
    executor_agent_type=AgentType.CLAUDE, capability_host="127.0.0.1", telethon_env=TELETHON
)


class FakeClient:
    def __init__(self, *, authorized=True, user_id=QA_TEST_TELEGRAM_ID, connect_error=None):
        self.authorized = authorized
        self.user_id = user_id
        self.connect_error = connect_error
        self.disconnected = False

    async def connect(self):
        if self.connect_error:
            raise self.connect_error

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return SimpleNamespace(id=self.user_id)

    async def disconnect(self):
        self.disconnected = True


class TestTheIdentityIsProvenForEveryRun:
    async def test_a_runtime_without_credentials_has_nothing_to_prove(self):
        runtime = QARuntimeConfig(executor_agent_type=AgentType.CLAUDE, capability_host="h")

        def factory(_environment):
            raise AssertionError("no client without credentials")

        proven = await prove_sandbox_telegram_identity(runtime, client_factory=factory)

        assert proven == runtime
        assert identity_record(proven) is None

    async def test_the_qa_account_is_proven_and_marked_for_handover(self):
        client = FakeClient()

        proven = await prove_sandbox_telegram_identity(RUNTIME, client_factory=lambda _e: client)

        assert proven.telegram_identity_proven is True
        assert proven.telethon_env == TELETHON
        assert client.disconnected
        assert identity_record(proven) == {"handed_over": True}

    @pytest.mark.parametrize(
        ("client", "reason", "detail"),
        [
            (
                FakeClient(authorized=False),
                "telethon_session_unauthorized",
                "the session is not authorized",
            ),
            (
                FakeClient(user_id=QA_TEST_TELEGRAM_ID + 1),
                "telethon_identity_mismatch",
                "the session is not the QA identity the QA runtime's /start probe expects",
            ),
            (
                FakeClient(connect_error=ConnectionError(f"refused for {SESSION}")),
                "telethon_session_unauthorized",
                "connect failed: ConnectionError",
            ),
        ],
        ids=["unauthorized", "another-account", "cannot-connect"],
    )
    async def test_a_session_that_fails_the_proof_is_withdrawn_for_the_whole_run(
        self, client, reason, detail
    ):
        withdrawn = await prove_sandbox_telegram_identity(RUNTIME, client_factory=lambda _e: client)

        # Exactly a runtime without Telethon credentials: neither the sandbox
        # nor the runtime's own Telegram tools get this session.
        assert withdrawn.telethon_env is None
        assert withdrawn.telegram_identity_proven is False
        assert client.disconnected
        record = identity_record(withdrawn)
        assert record == {"handed_over": False, "reason": reason, "detail": detail}
        assert SESSION not in json.dumps(record)

    async def test_a_session_telethon_cannot_load_is_refused_without_quoting_it(self):
        def factory(_environment):
            raise ValueError(f"Not a valid string: {SESSION}")

        withdrawn = await prove_sandbox_telegram_identity(RUNTIME, client_factory=factory)

        assert identity_record(withdrawn) == {
            "handed_over": False,
            "reason": "telethon_session_unauthorized",
            "detail": "the session could not be loaded: ValueError",
        }

    async def test_a_refused_session_fails_a_bot_run_as_missing_credentials_with_the_reason(
        self,
    ):
        withdrawn = await prove_sandbox_telegram_identity(
            RUNTIME, client_factory=lambda _e: FakeClient(authorized=False)
        )

        blocker = await preflight_bot_access(
            bot_username="product_bot",
            telethon_env=withdrawn.telethon_env,
            identity_refusal=withdrawn.telegram_identity_refusal,
        )

        assert blocker.category is QABlockerCategory.MISSING_TELETHON_CREDENTIALS
        assert "telethon_session_unauthorized" in blocker.received


@pytest.fixture
async def identity_endpoint(tmp_path):
    """An endpoint of a run whose identity was proven, or of one where it was refused."""
    started: list[QACapabilityService] = []

    async def _start(*, proven: bool, calls=None):
        service = QACapabilityService(
            calls=calls or {},
            capabilities={},
            submit_verdict=lambda _raw: None,
            advertised_host="127.0.0.1",
            telegram_identity=TELETHON if proven else None,
            telegram_identity_refusal=None if proven else "telethon_identity_mismatch: other",
        )
        endpoint = await service.start()
        started.append(service)
        return service, endpoint

    yield _start
    for service in started:
        await service.stop()


async def _post(endpoint, tool, *, token=None):
    headers = {"Authorization": f"Bearer {token or endpoint.token}"}
    async with aiohttp.ClientSession() as http:
        async with http.post(
            endpoint.url, json={"tool": tool, "args": {}}, headers=headers
        ) as response:
            return response.status, await response.json()


class TestTheEndpointServesOnlyAProvenIdentity:
    async def test_the_run_token_gets_the_proven_identity(self, identity_endpoint):
        service, endpoint = await identity_endpoint(proven=True)

        status, body = await _post(endpoint, TELEGRAM_IDENTITY_CALL)

        assert status == 200
        assert body == {
            "tool": TELEGRAM_IDENTITY_CALL,
            "api_id": "12345",
            "api_hash": API_HASH,
            "session": SESSION,
            "user_id": QA_TEST_TELEGRAM_ID,
        }
        # Asking for the identity is not the executor reaching the product.
        assert service.calls_served == 0

    async def test_another_token_gets_nothing(self, identity_endpoint):
        _, endpoint = await identity_endpoint(proven=True)

        status, body = await _post(endpoint, TELEGRAM_IDENTITY_CALL, token="another-run")  # noqa: S106

        assert status == 401
        assert SESSION not in json.dumps(body)

    async def test_a_refused_identity_is_named_and_never_served(self, identity_endpoint):
        _, endpoint = await identity_endpoint(proven=False)

        status, body = await _post(endpoint, TELEGRAM_IDENTITY_CALL)

        assert status == 400
        assert "telethon_identity_mismatch" in body["error"]
        assert SESSION not in json.dumps(body)


class TestTheQaCommandKeepsTheSessionOutOfItsOutput:
    async def test_the_identity_lands_in_a_private_file_and_only_the_path_is_printed(
        self, identity_endpoint, tmp_path
    ):
        _, endpoint = await identity_endpoint(proven=True)
        script = tmp_path / "qa"
        script.write_text(QA_PROBE_SCRIPT)
        home = tmp_path / "home"
        home.mkdir()

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
            "telegram_identity",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(home),
                "QA_CAPABILITY_URL": endpoint.url,
                "QA_CAPABILITY_TOKEN": endpoint.token,
                "HTTPS_PROXY": "http://qa-egress-qa-1:3128",
            },
        )
        stdout, stderr = await process.communicate()

        assert process.returncode == 0, stderr
        printed = stdout.decode() + stderr.decode()
        assert SESSION not in printed
        assert API_HASH not in printed
        answer = json.loads(stdout)
        path = Path(answer["file"])
        assert path == home / ".qa" / "telegram_identity.json"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert json.loads(path.read_text()) == {
            "api_id": 12345,
            "api_hash": API_HASH,
            "session": SESSION,
            "user_id": QA_TEST_TELEGRAM_ID,
            "proxy": ["http", "qa-egress-qa-1", 3128],
        }


class TestWhatTheSandboxSaysBackIsScrubbed:
    def test_only_a_handed_over_identity_is_scrubbed(self):
        assert handed_over_secrets(RUNTIME) == ()
        proven = QARuntimeConfig(
            executor_agent_type=AgentType.CLAUDE,
            capability_host="h",
            telethon_env=TELETHON,
            telegram_identity_proven=True,
        )

        secrets = handed_over_secrets(proven)
        said = f"cat identity: {{'session': '{SESSION}', 'api_hash': '{API_HASH}'}}"

        assert redact(said, secrets) == (
            f"cat identity: {{'session': '{REDACTED}', 'api_hash': '{REDACTED}'}}"
        )


async def test_the_create_request_carries_the_target_as_data_and_the_sandbox_tooling():
    published: list[CreateWorkerCommand] = []
    redis_client = AsyncMock()

    async def xadd(_stream, fields, **_kwargs):
        if not published:
            published.append(CreateWorkerCommand.model_validate_json(fields["data"]))

    redis_client.xadd.side_effect = xadd
    with (
        patch("src.clients.qa_worker.redis.from_url", return_value=redis_client),
        patch(
            "src.clients.qa_worker._wait_for_response",
            new_callable=AsyncMock,
            side_effect=RuntimeError("stop after create"),
        ),
        pytest.raises(RuntimeError, match="stop after create"),
    ):
        await run_qa_executor(
            agent_type=AgentType.CODEX,
            ownership=WorkerOwnership(
                story_id="story-1", project_id="project-1", run_id="qa-1", attempt_id="qa-1"
            ),
            deploy_target_url="http://95.216.10.20:8080",
            capability_url="http://qa-worker:41234/qa/call",
            capability_token="run-token",  # noqa: S106 - fake endpoint credential
            instructions="# QA executor",
            prompt="test it",
            verdict_received=asyncio.Event(),
            calls_served=lambda: 0,
            timeout=1,
            on_create_published=lambda: None,
        )

    [command] = published
    assert command.config.qa_target_url == "http://95.216.10.20:8080"
    assert [capability.value for capability in command.config.capabilities] == ["qa_sandbox"]
    # The environment is the capability endpoint and nothing else: no Telegram
    # credential and no deploy target ride along as variables.
    assert command.config.env_vars == {
        "QA_CAPABILITY_URL": "http://qa-worker:41234/qa/call",
        "QA_CAPABILITY_TOKEN": "run-token",
    }
