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

    def test_checks_budget_before_starting_paid_work(self):
        assert "get_budget_balance" in SYSTEM_PROMPT
        assert "attempt_reservation_microusd" in SYSTEM_PROMPT
        assert "remaining_microusd" in SYSTEM_PROMPT
        assert "unknown_cost_attempt_count" in SYSTEM_PROMPT

    def test_contains_env_hints_instructions(self):
        """Prompt should instruct PO to use hint parameter with set_project_secret."""
        assert "hint" in SYSTEM_PROMPT.lower()
        assert "set_project_secret" in SYSTEM_PROMPT

    def test_describes_durable_permanent_access_tools(self):
        assert "grant_project_user" in SYSTEM_PROMPT
        assert "transfer_project_ownership" in SYSTEM_PROMPT
        assert "active readback" in SYSTEM_PROMPT
        assert "allowed Telegram IDs" not in SYSTEM_PROMPT
        assert "bot admin ID" not in SYSTEM_PROMPT

    def test_mentions_user_context(self):
        """Prompt should reference the user context prefix (chat id, user name)."""
        assert "telegram_chat_id" in SYSTEM_PROMPT
        assert "context" in SYSTEM_PROMPT.lower()

    def test_story_based_workflow(self):
        """Prompt should reference story-based workflow."""
        assert "story" in SYSTEM_PROMPT.lower()
        assert "create_story" in SYSTEM_PROMPT

    def test_sends_one_confirmation_summary_before_creating_a_story(self):
        """A project brief is confirmed once, rather than interrogated piecemeal."""
        assert "exactly one structured summary" in SYSTEM_PROMPT
        assert "intended users" in SYSTEM_PROMPT
        assert "languages" in SYSTEM_PROMPT
        assert "must-requirements" in SYSTEM_PROMPT
        assert "yes / correct me" in SYSTEM_PROMPT

    def test_the_confirmation_is_the_product_brief_flow(self):
        """The confirmed brief, not a re-worded summary, is what is planned against."""
        assert "## The Product Brief: Confirmation Before Creating a Story" in SYSTEM_PROMPT
        assert "present_product_brief" in SYSTEM_PROMPT
        assert "confirm_product_brief" in SYSTEM_PROMPT
        assert "product_brief_id=<the brief id>" in SYSTEM_PROMPT

    def test_a_correction_is_a_new_revision(self):
        assert "corrects_brief_id" in SYSTEM_PROMPT
        assert "A correction is a new revision, never an edit" in SYSTEM_PROMPT

    def test_the_stored_revision_is_never_re_composed(self):
        assert "do not compose another one" in SYSTEM_PROMPT

    def test_a_secret_is_never_an_initial_setting(self):
        assert "NEVER put a token, password or API key into `initial_settings`" in SYSTEM_PROMPT

    def test_the_flows_that_have_no_brief_keep_working(self):
        assert "A `fix` story on an existing project and `reopen_story` need no brief" in (
            SYSTEM_PROMPT
        )

    def test_a_feature_on_a_live_project_is_new_product_work_too(self):
        """The shape most product work takes once a project exists."""
        assert "the first story of a new project and every later feature alike" in SYSTEM_PROMPT
        assert "**A new feature**: it is new product work" in SYSTEM_PROMPT
        assert "Each feature gets its own brief" in SYSTEM_PROMPT

    def test_offers_both_ways_out_of_an_own_token_conflict(self):
        """Without this, the agent asks for another token the user does not have."""
        assert "teardown_project" in SYSTEM_PROMPT
        assert "Continue there" in SYSTEM_PROMPT
        assert "Free the token" in SYSTEM_PROMPT

    def test_teardown_needs_the_users_consent(self):
        assert "Never call `teardown_project` on your own initiative" in SYSTEM_PROMPT

    def test_the_token_is_rebound_only_after_the_teardown_confirms(self):
        """Rebinding while the old bot still polls loses the race with Telegram."""
        assert "ONLY after that tool reports the bot free" in SYSTEM_PROMPT
        assert "still shutting down" in SYSTEM_PROMPT

    def test_no_trigger_engineering_references(self):
        """Prompt should not reference deprecated trigger_engineering."""
        assert "trigger_engineering" not in SYSTEM_PROMPT


class TestCreateStoryDocstring:
    """Tests for create_story tool docstring."""

    def test_mentions_gathered_requirements(self):
        doc = create_story.description
        assert "gathered requirements" in doc.lower() or "detailed" in doc.lower()

    def test_says_new_product_work_needs_a_confirmed_brief(self):
        doc = create_story.description
        assert "confirmed Product Brief" in doc
        assert "product_brief_id" in doc

    def test_says_a_feature_is_new_product_work_too(self):
        doc = create_story.description
        assert "the first story of a project and every later feature alike" in doc
        assert "leave unset only for a fix on an existing project" in doc
