"""Unit tests for telegram_bot settings validation."""

import os

os.environ.setdefault("LK_DOMAIN", "https://lk.test.example.com")

import pytest

from shared.contracts.vocab import AgentType
from src.config import Settings


def test_blank_lk_domain_is_rejected(monkeypatch):
    """Compose passes an unset LK_DOMAIN through as "" — that must not validate."""
    monkeypatch.setenv("LK_DOMAIN", "")

    with pytest.raises(ValueError, match="LK_DOMAIN|lk_domain"):
        Settings()


def test_lk_domain_is_read_from_env(monkeypatch):
    monkeypatch.setenv("LK_DOMAIN", "https://lk.example.com")

    assert Settings().lk_domain == "https://lk.example.com"


def test_missing_default_agent_type_is_rejected(monkeypatch):
    monkeypatch.delenv("DEFAULT_AGENT_TYPE", raising=False)

    with pytest.raises(ValueError, match="DEFAULT_AGENT_TYPE"):
        Settings(_env_file=None)


def test_explicit_default_agent_type_is_accepted(monkeypatch):
    monkeypatch.setenv("DEFAULT_AGENT_TYPE", "codex")

    assert Settings(_env_file=None).default_agent_type is AgentType.CODEX


def test_bot_token_stays_required(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    with pytest.raises(ValueError, match="telegram_bot_token"):
        Settings(_env_file=None)


@pytest.mark.parametrize("value", [None, "0", "1", "invalid"])
def test_concurrent_update_policy_is_required_and_at_least_two(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("TELEGRAM_MAX_CONCURRENT_UPDATES", raising=False)
    else:
        monkeypatch.setenv("TELEGRAM_MAX_CONCURRENT_UPDATES", value)
    with pytest.raises(ValueError, match="TELEGRAM_MAX_CONCURRENT_UPDATES"):
        Settings(_env_file=None)
