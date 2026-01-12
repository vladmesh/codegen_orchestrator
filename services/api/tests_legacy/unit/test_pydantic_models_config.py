"""Unit tests for Pydantic model configuration.

Regression tests for commit 6b8ae45 - Pydantic deprecation warnings.
Tests ensure all models use modern ConfigDict instead of deprecated class Config.
"""

from pydantic import BaseModel

from src.schemas.agent_config import AgentConfigRead
from src.schemas.cli_agent_config import CLIAgentConfigRead


def test_agent_config_read_uses_config_dict():
    """Test that AgentConfigRead uses model_config = ConfigDict().

    Regression test: Previously used deprecated `class Config` which caused warnings.
    """
    # Should have model_config attribute (Pydantic v2 style)
    assert hasattr(AgentConfigRead, "model_config")

    # Should NOT have Config inner class (Pydantic v1 style)
    assert not hasattr(AgentConfigRead, "Config")


def test_cli_agent_config_read_uses_config_dict():
    """Test that CLIAgentConfigRead uses model_config = ConfigDict().

    Regression test: Previously used deprecated `class Config`.
    """
    # Should have model_config attribute
    assert hasattr(CLIAgentConfigRead, "model_config")

    # Should NOT have Config inner class
    assert not hasattr(CLIAgentConfigRead, "Config")


def test_agent_config_read_from_attributes():
    """Test that AgentConfigRead can be created from ORM objects.

    The from_attributes=True setting allows creating models from SQLAlchemy objects.
    """
    # Verify from_attributes is set in model_config
    config = AgentConfigRead.model_config
    assert config.get("from_attributes") is True


def test_cli_agent_config_read_from_attributes():
    """Test that CLIAgentConfigRead can be created from ORM objects."""
    config = CLIAgentConfigRead.model_config
    assert config.get("from_attributes") is True


def test_models_are_pydantic_v2_compatible():
    """Verify models don't use any Pydantic v1-only syntax."""
    # Both should be proper Pydantic BaseModel instances
    assert issubclass(AgentConfigRead, BaseModel)
    assert issubclass(CLIAgentConfigRead, BaseModel)

    # Should be able to instantiate with valid data
    agent_config = AgentConfigRead(
        id="test_agent",
        name="Test Agent",
        system_prompt="Test prompt",
        model="gpt-4",
        version=1,
    )

    assert agent_config.id == "test_agent"
    assert agent_config.version == 1

    cli_config = CLIAgentConfigRead(
        id="test_cli",
        name="Test CLI Agent",
        provider="factory",
        prompt_template="Test prompt",
        version=2,
    )

    assert cli_config.id == "test_cli"
