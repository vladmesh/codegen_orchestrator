"""`agent_configs.llm_channels`: the API accepts only a chain an agent can start with."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
from internal_caller import INTERNAL_HEADERS
import pytest

from shared.models import AgentConfig
from src.database import get_async_session
from src.main import app


def _session(existing: AgentConfig | None = None):
    session = AsyncMock()
    session.get = AsyncMock(return_value=existing)
    session.add = MagicMock()
    session.commit = AsyncMock()

    async def _refresh(obj):
        now = datetime.now(UTC)
        obj.created_at = obj.created_at if getattr(obj, "created_at", None) else now
        obj.updated_at = now
        obj.version = obj.version or 1

    session.refresh = _refresh
    return session


def _existing(**overrides) -> AgentConfig:
    config = AgentConfig(
        id="architect",
        name="Architect",
        system_prompt="unused",
        model_name="gpt-4o",
        temperature=0.0,
        llm_provider="openrouter",
        model_identifier="openai/gpt-4o",
        is_active=True,
        version=1,
        llm_channels=None,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    app.dependency_overrides.clear()


async def _send(method: str, path: str, session, body: dict):
    app.dependency_overrides[get_async_session] = lambda: session
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        return await client.request(method, path, json=body)


_CREATE = {"id": "architect", "name": "Architect", "system_prompt": "unused"}


async def test_create_stores_the_validated_chain_in_order():
    session = _session()
    chain = [
        {"channel": "claude", "model": "claude-sonnet-5"},
        {"channel": "openrouter", "model": "openai/gpt-5.6-sol", "timeout_seconds": 300},
    ]

    resp = await _send("POST", "/api/agent-configs/", session, {**_CREATE, "llm_channels": chain})

    assert resp.status_code == 201, resp.text
    stored = session.add.call_args[0][0]
    assert stored.llm_channels == chain
    assert resp.json()["llm_channels"] == [
        {"channel": "claude", "model": "claude-sonnet-5", "timeout_seconds": None},
        {"channel": "openrouter", "model": "openai/gpt-5.6-sol", "timeout_seconds": 300.0},
    ]


async def test_create_without_a_chain_stores_null_for_the_default_chain():
    session = _session()

    resp = await _send("POST", "/api/agent-configs/", session, _CREATE)

    assert resp.status_code == 201, resp.text
    assert session.add.call_args[0][0].llm_channels is None
    assert resp.json()["llm_channels"] is None


@pytest.mark.parametrize(
    ("chain", "reason"),
    [
        ([], "at least one channel"),
        ([{"channel": "gemini"}], "codex"),
        ([{"channel": "codex"}, {"channel": "codex", "model": "gpt-5"}], "each channel once"),
        ([{"channel": "codex", "api_key": "sk-x"}], "extra"),
        ([{"channel": "claude", "model": ""}], "at least 1 character"),
    ],
    ids=["empty", "unknown-channel", "duplicate", "unknown-field", "empty-model"],
)
async def test_create_and_patch_refuse_an_invalid_chain(chain, reason):
    create_session = _session()
    created = await _send(
        "POST", "/api/agent-configs/", create_session, {**_CREATE, "llm_channels": chain}
    )
    patch_session = _session(existing=_existing())
    patched = await _send(
        "PATCH", "/api/agent-configs/architect", patch_session, {"llm_channels": chain}
    )

    for resp in (created, patched):
        assert resp.status_code == 422
        assert reason in resp.text
    create_session.add.assert_not_called()
    patch_session.commit.assert_not_awaited()


async def test_patch_replaces_the_chain_and_null_restores_the_default():
    config = _existing(llm_channels=[{"channel": "openrouter"}])
    session = _session(existing=config)

    resp = await _send(
        "PATCH",
        "/api/agent-configs/architect",
        session,
        {"llm_channels": [{"channel": "codex", "model": "gpt-5.5"}, {"channel": "openrouter"}]},
    )

    assert resp.status_code == 200, resp.text
    assert config.llm_channels == [
        {"channel": "codex", "model": "gpt-5.5"},
        {"channel": "openrouter"},
    ]
    assert config.version == 2  # noqa: PLR2004

    resp = await _send("PATCH", "/api/agent-configs/architect", session, {"llm_channels": None})

    assert resp.status_code == 200, resp.text
    assert config.llm_channels is None
