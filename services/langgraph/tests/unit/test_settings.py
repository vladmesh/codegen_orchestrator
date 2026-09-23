"""Unit tests for langgraph settings validation."""

import pytest

from shared.contracts.vocab import AgentType
from src.config.settings import Settings


def test_missing_default_agent_type_is_rejected(monkeypatch):
    """The engineering executor is production policy; no value may be assumed."""
    monkeypatch.delenv("DEFAULT_AGENT_TYPE", raising=False)

    with pytest.raises(ValueError, match="DEFAULT_AGENT_TYPE"):
        Settings(_env_file=None)


def test_explicit_default_agent_type_is_accepted(monkeypatch):
    monkeypatch.setenv("DEFAULT_AGENT_TYPE", "factory")

    assert Settings(_env_file=None).default_agent_type is AgentType.FACTORY
