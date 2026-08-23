"""The PO prompt routes "add user ID X" / "remove user ID X" to the typed tools."""

from src.prompts.po import SYSTEM_PROMPT


class TestBotUserAudienceTools:
    def test_names_both_conversation_tools(self):
        assert "add_bot_user" in SYSTEM_PROMPT
        assert "remove_bot_user" in SYSTEM_PROMPT

    def test_tells_the_agent_never_to_reconstruct_the_audience_list(self):
        assert "NEVER reconstruct" in SYSTEM_PROMPT or "never reconstruct" in SYSTEM_PROMPT

    def test_keeps_set_bot_access_for_the_initial_selection(self):
        assert "set_bot_access" in SYSTEM_PROMPT

    def test_the_owner_stays_in_the_audience_unless_acted_on(self):
        assert "owner remains in the audience" in SYSTEM_PROMPT

    def test_live_access_is_claimed_only_after_rollout_confirmation(self):
        assert "rollout" in SYSTEM_PROMPT.lower()
        assert "applied" in SYSTEM_PROMPT
