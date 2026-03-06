"""Unit tests for PO system prompt and tool docstrings."""

from src.po.prompts import SYSTEM_PROMPT
from src.po.tools import trigger_engineering

MAX_PROMPT_LENGTH = 8000


class TestSystemPrompt:
    """Tests for SYSTEM_PROMPT content and quality."""

    def test_contains_requirements_gathering_section(self):
        assert "## Requirements Gathering" in SYSTEM_PROMPT

    def test_instructs_when_to_clarify(self):
        assert (
            "When to clarify" in SYSTEM_PROMPT
        ), "Prompt should explain when to ask follow-up questions"

    def test_instructs_when_to_just_go(self):
        assert (
            "When to just go" in SYSTEM_PROMPT
        ), "Prompt should explain when to skip clarification"

    def test_non_technical_focus(self):
        assert "non-technical" in SYSTEM_PROMPT.lower(), "Prompt should mention non-technical users"
        assert "Do NOT ask about technical details" in SYSTEM_PROMPT

    def test_mentions_structured_description(self):
        assert (
            "description" in SYSTEM_PROMPT.lower()
        ), "Prompt should reference passing gathered requirements as description"

    def test_prompt_length_sanity(self):
        assert (
            len(SYSTEM_PROMPT) < MAX_PROMPT_LENGTH
        ), f"Prompt is {len(SYSTEM_PROMPT)} chars, should be under {MAX_PROMPT_LENGTH}"

    def test_preserves_existing_scenarios(self):
        assert "## Scenario: User Wants to Create a NEW Bot/Project" in SYSTEM_PROMPT
        assert "## Scenario: User Wants to REDEPLOY" in SYSTEM_PROMPT
        assert "## Scenario: User Wants to ADD FEATURES or FIX BUGS" in SYSTEM_PROMPT

    def test_preserves_system_events_section(self):
        assert "## System Events & Reminders" in SYSTEM_PROMPT

    def test_preserves_key_principles(self):
        assert "## Key Principles" in SYSTEM_PROMPT
        assert "NEVER write code yourself" in SYSTEM_PROMPT


class TestTriggerEngineeringDocstring:
    """Tests for trigger_engineering tool docstring."""

    def test_mentions_gathered_requirements(self):
        doc = trigger_engineering.description
        assert "gathered requirements" in doc.lower() or "detailed" in doc.lower()
