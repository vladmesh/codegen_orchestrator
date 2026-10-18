"""Unit tests for API settings validation."""

import pytest

from shared.contracts.dto.executor_decision import ExecutorDecisionSource
from shared.contracts.dto.run import RunType
from shared.contracts.vocab import AgentType
from src.config import Settings
from src.executor_resolver import resolve_executor_decision


def test_blank_lk_jwt_secret_is_rejected(monkeypatch):
    """An empty LK_JWT_SECRET would sign dashboard tokens with a known key."""
    monkeypatch.setenv("LK_JWT_SECRET", "")

    with pytest.raises(ValueError, match="LK_JWT_SECRET|lk_jwt_secret"):
        Settings()


def test_missing_default_agent_type_is_rejected(monkeypatch):
    """The engineering executor is production policy; no value may be assumed."""
    monkeypatch.delenv("DEFAULT_AGENT_TYPE", raising=False)

    with pytest.raises(ValueError, match="DEFAULT_AGENT_TYPE"):
        Settings(_env_file=None)


def test_blank_default_agent_type_is_rejected(monkeypatch):
    """Compose passes an unset variable through env_file as "" — that must not validate."""
    monkeypatch.setenv("DEFAULT_AGENT_TYPE", "")

    with pytest.raises(ValueError, match="DEFAULT_AGENT_TYPE"):
        Settings(_env_file=None)


@pytest.mark.parametrize("agent", ["claude", "factory", "codex"])
def test_explicit_default_agent_type_is_accepted(monkeypatch, agent):
    monkeypatch.setenv("DEFAULT_AGENT_TYPE", agent)

    assert Settings(_env_file=None).default_agent_type is AgentType(agent)


def test_settings_and_executor_resolution_work_without_telegram_bot_token(monkeypatch):
    """The API has no Telegram token setting; the bot service owns that credential."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("DEFAULT_AGENT_TYPE", "codex")

    settings = Settings(_env_file=None)
    decision = resolve_executor_decision(RunType.ENGINEERING, None, settings)

    assert decision.agent_type is AgentType.CODEX
    assert decision.source is ExecutorDecisionSource.API_DEFAULT


def test_blank_internal_api_key_is_rejected(monkeypatch):
    """An empty INTERNAL_API_KEY would authenticate an empty X-Internal-Key header."""
    monkeypatch.setenv("INTERNAL_API_KEY", "")

    with pytest.raises(ValueError, match="INTERNAL_API_KEY"):
        Settings(_env_file=None)


def test_missing_github_webhook_secret_is_rejected(monkeypatch):
    """Webhook authentication has no disabled mode: a missing secret fails startup."""
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)

    with pytest.raises(ValueError, match="GITHUB_WEBHOOK_SECRET"):
        Settings(_env_file=None)


@pytest.mark.parametrize("value", ["", " ", "\t\n "])
def test_blank_github_webhook_secret_is_rejected(monkeypatch, value):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", value)

    with pytest.raises(ValueError, match="GITHUB_WEBHOOK_SECRET"):
        Settings(_env_file=None)


def test_github_webhook_secret_is_masked_outside_verification(monkeypatch):
    value = "synthetic-webhook-signing-value"
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", value)

    settings = Settings(_env_file=None)

    assert settings.github_webhook_secret.get_secret_value() == value
    for rendered in (repr(settings), str(settings), settings.model_dump_json()):
        assert value not in rendered
