"""Tests for E2E prompts."""

import re

import pytest

from tests.e2e.e2e_prompt import (
    EXPECTED_PATTERNS,
    build_project_creation_prompt,
    build_simple_project_prompt,
)

pytestmark = pytest.mark.e2e


class TestPromptBuilder:
    """Tests for prompt builder functions."""

    def test_build_project_creation_prompt_includes_project_name(self):
        """Prompt should include the project name."""
        prompt = build_project_creation_prompt("my-test-project")
        assert "my-test-project" in prompt

    def test_build_project_creation_prompt_includes_no_questions_instruction(self):
        """Prompt should instruct Claude not to ask questions."""
        prompt = build_project_creation_prompt("test")
        assert "НЕ задавай" in prompt or "не задавай" in prompt.lower()

    def test_build_project_creation_prompt_includes_commands(self):
        """Prompt should include expected CLI commands."""
        prompt = build_project_creation_prompt("test-project")
        assert "orchestrator project create" in prompt
        assert "orchestrator engineering trigger" in prompt
        assert "orchestrator respond" in prompt

    def test_build_project_creation_prompt_uses_custom_token(self):
        """Prompt should use provided telegram token."""
        prompt = build_project_creation_prompt("test", telegram_token="CUSTOM:TOKEN")  # noqa: S106
        assert "CUSTOM:TOKEN" in prompt

    def test_build_simple_project_prompt_is_minimal(self):
        """Simple prompt should be shorter than full prompt."""
        full = build_project_creation_prompt("test")
        simple = build_simple_project_prompt("test")
        assert len(simple) < len(full)

    def test_build_simple_project_prompt_no_engineering(self):
        """Simple prompt should not include engineering trigger."""
        prompt = build_simple_project_prompt("test")
        assert "engineering trigger" not in prompt


class TestExpectedPatterns:
    """Tests for regex patterns."""

    def test_project_create_pattern_matches(self):
        """Pattern should match project create command."""
        pattern = EXPECTED_PATTERNS["project_create"]
        assert re.search(pattern, "orchestrator project create --name my-bot")
        assert re.search(pattern, "orchestrator   project   create   --name   test")

    def test_engineering_trigger_pattern_matches(self):
        """Pattern should match engineering trigger command."""
        pattern = EXPECTED_PATTERNS["engineering_trigger"]
        assert re.search(pattern, "orchestrator engineering trigger abc-123")

    def test_respond_pattern_matches(self):
        """Pattern should match respond command."""
        pattern = EXPECTED_PATTERNS["respond"]
        assert re.search(pattern, 'orchestrator respond "Done"')
