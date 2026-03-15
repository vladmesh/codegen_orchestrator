"""Unit tests for PO system prompt and tool docstrings."""

from src.agents.po.tools import create_story
from src.prompts.po import SYSTEM_PROMPT

MAX_PROMPT_LENGTH = 14000


class TestSystemPrompt:
    """Tests for SYSTEM_PROMPT content and quality."""

    def test_contains_requirements_gathering_section(self):
        assert "## Requirements Gathering" in SYSTEM_PROMPT

    def test_instructs_when_to_clarify(self):
        assert "When to clarify" in SYSTEM_PROMPT, (
            "Prompt should explain when to ask follow-up questions"
        )

    def test_instructs_when_to_just_go(self):
        assert "When to just go" in SYSTEM_PROMPT, (
            "Prompt should explain when to skip clarification"
        )

    def test_non_technical_focus(self):
        assert "non-technical" in SYSTEM_PROMPT.lower(), "Prompt should mention non-technical users"
        assert "Do NOT ask about technical details" in SYSTEM_PROMPT

    def test_mentions_structured_description(self):
        assert "description" in SYSTEM_PROMPT.lower(), (
            "Prompt should reference passing gathered requirements as description"
        )

    def test_prompt_length_sanity(self):
        assert len(SYSTEM_PROMPT) < MAX_PROMPT_LENGTH, (
            f"Prompt is {len(SYSTEM_PROMPT)} chars, should be under {MAX_PROMPT_LENGTH}"
        )

    def test_preserves_existing_scenarios(self):
        assert "New Project" in SYSTEM_PROMPT
        assert "Add Features" in SYSTEM_PROMPT or "Fix Bugs" in SYSTEM_PROMPT
        assert "Status" in SYSTEM_PROMPT

    def test_preserves_reminders_section(self):
        assert "Reminders" in SYSTEM_PROMPT
        assert "set_reminder" in SYSTEM_PROMPT

    def test_preserves_key_principles(self):
        assert "## Key Principles" in SYSTEM_PROMPT
        assert "NEVER write code yourself" in SYSTEM_PROMPT

    def test_contains_env_hints_instructions(self):
        """Prompt should instruct PO to use hint parameter with set_project_secret."""
        assert "hint" in SYSTEM_PROMPT.lower()
        assert "set_project_secret" in SYSTEM_PROMPT

    def test_contains_access_control_question(self):
        """Prompt should ask about bot access control for tg_bot projects."""
        prompt_lower = SYSTEM_PROMPT.lower()
        assert "access" in prompt_lower
        assert "ADMIN_TELEGRAM_ID" in SYSTEM_PROMPT

    def test_access_control_options(self):
        """Prompt should list access control options."""
        prompt_lower = SYSTEM_PROMPT.lower()
        # All four options should be mentioned
        assert "only me" in prompt_lower or "только мне" in prompt_lower
        assert "everyone" in prompt_lower or "всем" in prompt_lower
        assert "admin" in prompt_lower

    def test_mentions_user_context(self):
        """Prompt should reference user context (user_id, user_name) from message prefix."""
        assert "user_id" in SYSTEM_PROMPT
        assert "context" in SYSTEM_PROMPT.lower()

    def test_story_based_workflow(self):
        """Prompt should reference story-based workflow."""
        assert "story" in SYSTEM_PROMPT.lower()
        assert "create_story" in SYSTEM_PROMPT

    def test_no_trigger_engineering_references(self):
        """Prompt should not reference deprecated trigger_engineering."""
        assert "trigger_engineering" not in SYSTEM_PROMPT


class TestCreateStoryDocstring:
    """Tests for create_story tool docstring."""

    def test_mentions_gathered_requirements(self):
        doc = create_story.description
        assert "gathered requirements" in doc.lower() or "detailed" in doc.lower()
