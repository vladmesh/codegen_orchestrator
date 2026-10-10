"""Fixed QA borrows only the exact native grant, and records no executor credentials."""

from datetime import UTC, datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fakeredis.aioredis import FakeRedis
import pytest

from shared.contracts.bot_access import QA_TEST_TELEGRAM_ID
from shared.contracts.dto.temporary_access import TemporaryAccessStatus
from shared.contracts.queues.qa import QAMessage
from src.consumers import mechanical_telegram, qa
from src.consumers._qa_redaction import QARunRedaction
from src.consumers._qa_runner import QAResult
from src.consumers._qa_telegram_lease import TelegramIdentityLease


def free_lease():
    """The QA account's identity lease, free: these cases are about the grant and probe."""
    return TelegramIdentityLease(FakeRedis(), QA_TEST_TELEGRAM_ID)


def message():
    return QAMessage(
        project_id="project",
        story_id="story",
        initiating_run_id="stand-run",
        run_id="qa-run",
        application_id=42,
        deployed_url="https://product.example",
        bot_username="stand_bot",
        acceptance_criteria="- GET /health returns 200\n- Stand mechanical notes: unique",
    )


def grant(msg):
    return SimpleNamespace(
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


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", TemporaryAccessStatus.REVOKED),
        ("external_id", "foreign"),
        ("target_application_id", 43),
        ("target_base_url", "https://foreign.example"),
        ("qa_run_id", "other-run"),
        ("qa_message", None),
    ],
)
async def test_foreign_or_ended_native_grant_never_opens_a_session(monkeypatch, field, value):
    msg = message()
    native = grant(msg)
    setattr(native, field, value)
    api = SimpleNamespace(
        get_temporary_access_grant=AsyncMock(return_value=native), patch=AsyncMock()
    )
    probe = AsyncMock()
    monkeypatch.setattr(qa, "api_client", api)
    monkeypatch.setattr(mechanical_telegram, "run_fixed_probe", probe)
    result = await qa._run_mechanical_qa(
        msg, ("notes", "unique"), {}, QAResult(passed=True), QARunRedaction(), free_lease()
    )
    assert not result.passed
    assert json.loads(result.report)["phase"] == "grant"
    probe.assert_not_awaited()
    api.patch.assert_not_awaited()


async def test_fixed_probe_stays_inside_grant_and_retains_only_safe_identity(monkeypatch):
    msg = message()
    native = grant(msg)
    api = SimpleNamespace(
        get_temporary_access_grant=AsyncMock(return_value=native), patch=AsyncMock()
    )

    async def probe(**kwargs):
        kwargs["evidence"].update(
            status="passed",
            phase="completed",
            identity=QA_TEST_TELEGRAM_ID,
            reflected="unretained-user-capability",
        )

    monkeypatch.setattr(qa, "api_client", api)
    monkeypatch.setattr(mechanical_telegram, "run_fixed_probe", probe)
    capability = "unretained-user-capability"
    result = await qa._run_mechanical_qa(
        msg,
        ("notes", "unique"),
        {"USER_IDENTITY_CAPABILITY": capability},
        QAResult(passed=True),
        QARunRedaction([capability]),
        free_lease(),
    )
    assert result.passed
    assert api.get_temporary_access_grant.await_count == 2
    assert json.loads(result.report)["grant_valid_through"]
    assert capability not in result.report
    assert capability not in str(api.patch.call_args)
    assert result.probe_runs is None


async def test_data_conversation_uses_same_native_grant_and_runtime_settings_secret(monkeypatch):
    from src.consumers import stand_conversation

    msg = message()
    native = grant(msg)
    api = SimpleNamespace(
        get_temporary_access_grant=AsyncMock(return_value=native), patch=AsyncMock()
    )

    async def probe(**kwargs):
        assert kwargs["stored"]["SETTINGS_WRITE_CAPABILITY"] == "runtime-only-setting-secret"
        kwargs["evidence"].update(
            status="passed", phase="completed", languages={"ru": True, "en": True}
        )

    monkeypatch.setattr(qa, "api_client", api)
    monkeypatch.setattr(stand_conversation, "run_probe", probe)
    result = await qa._run_mechanical_qa(
        msg,
        ("conversation", "platform-module"),
        {
            "USER_IDENTITY_CAPABILITY": "runtime-only-identity",
            "SETTINGS_WRITE_CAPABILITY": "runtime-only-setting-secret",
        },
        QAResult(passed=True),
        QARunRedaction(["runtime-only-identity", "runtime-only-setting-secret"]),
        free_lease(),
    )
    assert result.passed
    assert api.get_temporary_access_grant.await_count == 2
    assert json.loads(result.report)["languages"] == {"ru": True, "en": True}
    assert "runtime-only" not in result.report


async def test_bot_refusal_has_a_named_phase(monkeypatch):
    bot = SimpleNamespace(id=42)
    refused = SimpleNamespace(
        id=2, out=False, peer_id=SimpleNamespace(user_id=42), raw_text="Access denied"
    )
    client = SimpleNamespace(
        send_message=AsyncMock(
            return_value=SimpleNamespace(id=1, raw_text="/note", out=True, date=datetime.now(UTC))
        ),
        get_messages=AsyncMock(return_value=[refused]),
    )
    evidence = {}
    with pytest.raises(mechanical_telegram.ProbeFailure, match="notes_save: bot refused"):
        await mechanical_telegram.run_conversation(
            client, bot, None, mode="notes", marker="unique", headers={}, evidence=evidence
        )
    assert evidence["phase"] == "notes_save"


@pytest.mark.subprocess
@pytest.mark.parametrize("failure", ["identity", "timeout"])
async def test_probe_disconnects_after_identity_failure_or_timeout(monkeypatch, failure):
    import asyncio

    import telethon
    import telethon.sessions

    client = SimpleNamespace(disconnect=AsyncMock())
    monkeypatch.setattr(telethon, "TelegramClient", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(telethon.sessions, "StringSession", lambda value: value)
    monkeypatch.setattr(
        mechanical_telegram,
        "telethon_env",
        lambda: {
            "TELETHON_SESSION": "never-retain-session",
            "TELETHON_API_ID": "1",
            "TELETHON_API_HASH": "never-retain-hash",
        },
    )

    async def identity(_client):
        if failure == "timeout":
            await asyncio.sleep(10)
        raise mechanical_telegram.ProbeFailure("identity", "wrong account")

    monkeypatch.setattr(mechanical_telegram, "prove_qa_identity", identity)
    monkeypatch.setattr(mechanical_telegram, "PROBE_TIMEOUT", 0.01)
    evidence = {}
    with pytest.raises(mechanical_telegram.ProbeFailure, match="identity"):
        await mechanical_telegram.run_fixed_probe(
            mode="notes",
            marker="unique",
            bot_username="bot",
            deployed_url="http://unused",
            headers={},
            evidence=evidence,
            redaction=QARunRedaction(),
        )
    client.disconnect.assert_awaited_once()
    assert evidence["status"] == "failed"
    assert evidence["disconnect"] == "completed"
    assert "never-retain" not in str(evidence)


def _fake_telethon(monkeypatch, client):
    import telethon
    import telethon.sessions

    monkeypatch.setattr(telethon, "TelegramClient", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(telethon.sessions, "StringSession", lambda value: value)
    monkeypatch.setattr(
        mechanical_telegram,
        "telethon_env",
        lambda: {
            "TELETHON_SESSION": "never-retain-session",
            "TELETHON_API_ID": "1",
            "TELETHON_API_HASH": "never-retain-hash",
        },
    )


async def test_identity_refusal_keeps_its_class_and_reason_in_qa_evidence(monkeypatch):
    """Run 37451655856 recorded only `identity: ProbeFailure`; the refusal reason was lost."""
    from shared.telethon_identity import SESSION_UNAUTHORIZED, IdentityNotProven

    _fake_telethon(monkeypatch, SimpleNamespace(disconnect=AsyncMock()))

    async def refuse(_client):
        # A detail that quotes the session must not survive into evidence.
        raise IdentityNotProven(SESSION_UNAUTHORIZED, "connect failed: never-retain-session")

    monkeypatch.setattr(mechanical_telegram, "prove_qa_identity", refuse)
    msg = message()
    api = SimpleNamespace(
        get_temporary_access_grant=AsyncMock(return_value=grant(msg)), patch=AsyncMock()
    )
    monkeypatch.setattr(qa, "api_client", api)
    result = await qa._run_mechanical_qa(
        msg,
        ("notes", "unique"),
        {"USER_IDENTITY_CAPABILITY": "cap"},
        QAResult(passed=True),
        QARunRedaction(),
        free_lease(),
    )

    assert not result.passed
    evidence = json.loads(result.report)
    assert evidence["phase"] == "identity"
    assert evidence["failure_type"] == "IdentityNotProven"
    assert evidence["failure_cause"]["reason"] == SESSION_UNAUTHORIZED
    assert evidence["failure_cause"]["detail"].startswith("connect failed: ")
    (check,) = [c for c in result.checks if c["name"] == "mechanical Telegram probe"]
    assert check["detail"].startswith(f"identity: IdentityNotProven: {SESSION_UNAUTHORIZED}")
    assert "never-retain" not in result.report
    assert "never-retain" not in check["detail"]


async def test_unknown_probe_error_keeps_only_its_class(monkeypatch):
    _fake_telethon(monkeypatch, SimpleNamespace(disconnect=AsyncMock()))

    async def explode(_client):
        raise ValueError("never-retain-session leaked in a message")

    monkeypatch.setattr(mechanical_telegram, "prove_qa_identity", explode)
    evidence = {}
    with pytest.raises(mechanical_telegram.ProbeFailure) as raised:
        await mechanical_telegram.run_fixed_probe(
            mode="notes",
            marker="unique",
            bot_username="bot",
            deployed_url="http://unused",
            headers={},
            evidence=evidence,
            redaction=QARunRedaction(),
        )
    assert raised.value.cause == {"type": "ValueError"}
    assert evidence["failure_cause"] == {"type": "ValueError"}
    assert "never-retain" not in str(evidence) + str(raised.value)
