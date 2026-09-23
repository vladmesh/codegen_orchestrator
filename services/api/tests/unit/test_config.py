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

    assert not hasattr(settings, "telegram_bot_token")
    assert decision.agent_type is AgentType.CODEX
    assert decision.source is ExecutorDecisionSource.API_DEFAULT
