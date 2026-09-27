"""Unit tests for PO system prompt and tool docstrings."""

import inspect
import re

from src.agents.po.tools_briefs import present_product_brief
from src.agents.po.tools_stories import create_story
from src.prompts.po import SYSTEM_PROMPT

MAX_PROMPT_LENGTH = 15000


def _section(heading: str) -> str:
    start = SYSTEM_PROMPT.index(heading)
    end = SYSTEM_PROMPT.find("\n## ", start + len(heading))
    return SYSTEM_PROMPT[start:] if end == -1 else SYSTEM_PROMPT[start:end]


def _product_brief_section() -> str:
    return _section("## The Product Brief")


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
        # The tool renders the answer line in the user's language (ru: «да / поправить»),
        # so an English literal here would contradict what the user is shown.
        assert "already ends with the answer line in their language" in SYSTEM_PROMPT
        assert "yes / correct me" not in SYSTEM_PROMPT
        assert "not specified" not in SYSTEM_PROMPT

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

    def test_every_story_is_an_order_and_is_redone_by_a_reopen(self):
        workflow = " ".join(_section("## Story-Based Workflow").split())
        assert "Every piece of work the user orders is a **story** with a confirmed" in workflow
        assert "Work is redone by reopening its story, never by a new one." in workflow

    def test_the_fix_story_instruction_is_gone(self):
        """A story the PO starts on its own is nobody's order (story-4b5265a8)."""
        assert "fix story" not in SYSTEM_PROMPT.lower()
        assert "story_type" not in SYSTEM_PROMPT
        assert "`fix`" not in SYSTEM_PROMPT
        assert "retry provenance" not in SYSTEM_PROMPT

    def test_a_retry_and_a_complaint_reopen_the_original_story(self):
        scenario = " ".join(_section("## Scenario: Add Features or Fix Bugs").split())
        assert (
            "**A complaint about something built, or a retry after a failure**: "
            "`list_stories(project_id)` → `reopen_story` on the original story, with "
            "`user_report` (the user's words) for a complaint. Never a new story."
        ) in scenario
        events = " ".join(_section("## Story Events & Reminders").split())
        assert "A retry is `reopen_story(story_id)`" in events

    def test_a_service_matter_is_a_note_to_the_admins(self):
        section = " ".join(_section("## Service Matters").split())
        assert "`note_to_admins(text)`" in section
        assert "It starts no work and sends the user nothing" in section
        assert "never create or reopen a story for it" in section

    def test_the_tools_named_for_retries_and_notes_exist(self):
        from src.agents.po.tools import get_all_tools

        names = {tool.name for tool in get_all_tools()}
        assert {"list_stories", "reopen_story", "note_to_admins"} <= names

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

    def test_documented_brief_arguments_are_the_tool_signature(self):
        """The prompt's call and the tool drifted once (1293); this keeps them together."""
        section = _product_brief_section()
        call = re.search(r"`present_product_brief\(([^)]*)\)`", section)
        assert call is not None
        documented = [name.strip() for name in call.group(1).split(",")]
        parameters = [
            name
            for name in inspect.signature(present_product_brief.coroutine).parameters
            if name != "config"
        ]
        assert documented == parameters
        for name in parameters[3:]:
            assert f"`{name}`:" in section, f"{name} is not documented"
        for field in ("user_wording", "wording_reference", "user_facing", "requirement_id"):
            assert f"`{field}`" in section
        assert "`description`" in section
        assert "at least one per user-facing requirement" in section

    def test_fixes_the_form_of_every_input_and_its_symmetric_case(self):
        assert "For every input the product accepts, fix the form it takes" in SYSTEM_PROMPT
        assert "a command, free text, a button or a photo" in SYSTEM_PROMPT
        assert "Check the symmetric case" in SYSTEM_PROMPT
        assert "say whether incomes can too" in SYSTEM_PROMPT
        assert 'a usage example in that form, or an explicit "not supported"' in SYSTEM_PROMPT

    def test_names_the_quality_trade_off_of_a_cheaper_variant(self):
        assert "cheaper or free variant that is noticeably worse" in SYSTEM_PROMPT
        assert "say the trade-off in one sentence and what can be connected later" in (
            SYSTEM_PROMPT
        )
        assert "Record it in `limitations`" in SYSTEM_PROMPT

    def test_a_stopped_story_is_reported_honestly(self):
        events = _section("## Story Events & Reminders")
        assert "work is stopped, a person is needed, there is no known time" in events
        assert (
            "Do NOT call it tested, finished, standard, a routine procedure or a specialist check"
        ) in events
        assert "do NOT say someone is checking or reviewing it, unless a tool result says so" in (
            events
        )
        assert "`waiting_human_review` — blocked → say work is stopped, a person is needed" in (
            events
        )

    def test_a_returned_requirement_is_told_plainly_with_a_follow_up_offer(self):
        events = _section("## Story Events & Reminders")
        bullet = events[events.index("- `story_requirements_returned`") :]
        bullet = bullet[: bullet.index("\n- ")] if "\n- " in bullet else bullet
        assert "will NOT be built in this story" in bullet
        assert "in their language, without jargon" in bullet
        assert "which part will not be built and why, and that the rest is being built" in bullet
        assert "Offer to settle that part as a follow-up feature" in bullet
        assert "confirm a corrected brief for it as its own story" in bullet
        assert "Never call it built, tested or under review" in bullet

    def test_a_quarantined_story_is_listed_with_the_blocked_wording(self):
        events = _section("## Story Events & Reminders")
        listed_part = events.split("**Reminders**")[0]
        assert re.search(r"^- `story_quarantined` —", listed_part, flags=re.MULTILINE)
        bullet = events[events.index("- `story_quarantined`") :]
        bullet = " ".join(bullet[: bullet.index("\n- ")].split())
        assert bullet == (
            "- `story_quarantined` — as `story_blocked`: work is stopped, a person decides, "
            "there is no known time."
        )

    def test_unverified_checks_get_one_honest_message_and_a_recorded_answer(self):
        events = " ".join(_section("## Story Events & Reminders").split())
        rule = events[events.index("**Checks QA could not run.**") :]
        rule = rule[: rule.index("**Reminders**")].strip()
        assert rule == (
            "**Checks QA could not run.** When `story_completed` or `story_quarantined` lists "
            '"What QA could not check", send ONE message in the user\'s language, with the '
            "event's news: (1) what was checked, briefly; (2) what could not be checked and "
            "why, in plain words, no ids or jargon; (3) ask them to choose: accept it "
            "unchecked, or change the requirement. Never call it tested. Record the answer "
            "with `record_unverified_decision`."
        )

    def test_the_rule_reads_the_headings_the_consumer_renders(self):
        from src.consumers.po import render_qa_verification

        rendered = render_qa_verification(
            {
                "qa_run_id": "qa-1",
                "passed_checks": ["a"],
                "unverified_checks": [{"name": "b", "reason": "c", "origin": "executor"}],
            }
        )
        assert "What QA could not check:" in rendered
        assert '"What QA could not check"' in SYSTEM_PROMPT

    def test_the_answer_tool_named_in_the_prompt_exists(self):
        from src.agents.po.tools import get_all_tools

        assert "record_unverified_decision" in {tool.name for tool in get_all_tools()}

    def test_a_completed_bot_is_explained_with_its_usage_instructions(self):
        events = _section("## Story Events & Reminders")
        bullet = events[events.index("- `story_completed`") :]
        bullet = bullet[: bullet.index("\n- ")]
        assert "relay the usage instructions from the event in the user's language" in bullet
        assert "how to reach the bot (@username)" in bullet
        assert "what they send and what the bot answers" in bullet.replace("\n", " ")
        assert "Never give a backend API address" in bullet
        assert "**NEVER fabricate URLs.**" in SYSTEM_PROMPT
        assert "Terminal stories: reply nothing" in SYSTEM_PROMPT

    def test_the_only_events_listed_are_the_owner_notification_vocabulary(self):
        """Drift: every event the prompt says it receives is one PO's consumer routes."""
        from shared.contracts.vocab import OwnerNotificationEvent

        events = _section("## Story Events & Reminders")
        listed_part = events.split("**Reminders**")[0]
        assert "Resource/infrastructure waits and resumptions: reply nothing" in listed_part
        listed = re.findall(r"^- `([a-z_]+)` —", listed_part, flags=re.MULTILINE)
        assert "story_requirements_returned" in listed
        assert "story_quarantined" in listed
        assert set(listed) <= {event.value for event in OwnerNotificationEvent}

    def test_a_problem_is_reported_with_its_cause_not_as_progress(self):
        """Incident 2026-09-24: an hour of "development continues" over a dead scaffold."""
        section = _section("## Reporting a Problem Honestly")
        assert "`get_story_diagnostics(story_id)`" in section
        assert "the cause in one plain sentence" in section
        assert "NEVER say development continues or nothing is required of them" in section
        events = _section("## Story Events & Reminders")
        assert "planning failed" in events
        assert "Reporting a Problem Honestly" in events

    def test_the_diagnostics_tool_named_in_the_prompt_exists(self):
        from src.agents.po.tools import get_all_tools

        assert "get_story_diagnostics" in {tool.name for tool in get_all_tools()}

    def test_the_old_reassuring_blocked_wording_is_gone(self):
        assert "specialist is looking into it" not in SYSTEM_PROMPT
        assert "this is normal" not in SYSTEM_PROMPT
        assert "specialist is reviewing" not in SYSTEM_PROMPT

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

    def test_points_a_retry_or_complaint_to_reopen_story(self):
        doc = create_story.description
        assert "Every story needs a confirmed Product Brief" in doc
        assert "use `reopen_story` on the original story" in doc
        assert "fix" not in doc.lower()


def test_story_status_is_only_answered_on_request():
    assert "brief update" not in SYSTEM_PROMPT
    assert "set_reminder(10" not in SYSTEM_PROMPT
    assert "Set a reminder for 10-15 minutes" not in SYSTEM_PROMPT
    assert "Do not set progress reminders after creating a story" in SYSTEM_PROMPT
    assert "in_work: reply nothing" in SYSTEM_PROMPT
    table = _section("## Situation → Tool")
    assert "| Status question | `get_product_situation(project_id)`, only when the user asks |" in (
        table
    )
    assert "Never push progress updates." in table
    assert "## Scenario: Status Check" not in SYSTEM_PROMPT


class TestSituationToTool:
    """The prompt's short "situation -> tool" table (sprint:1468 DoD item 3)."""

    def _rows(self) -> dict[str, str]:
        table = _section("## Situation → Tool")
        rows = re.findall(r"^\| (.+?) \| (.+?) \|$", table, flags=re.MULTILINE)
        return {situation: tool for situation, tool in rows if situation != "Situation"}

    def test_each_situation_names_its_tool(self):
        assert self._rows() == {
            "Status question": "`get_product_situation(project_id)`, only when the user asks",
            "A complaint about an ordered story": (
                "`list_stories` → `reopen_story` with `user_report`"
            ),
            "The user returns after a pause": (
                "`get_product_situation` first: its deferred notices"
            ),
        }

    def test_every_tool_the_table_names_exists(self):
        from src.agents.po.tools import get_all_tools
        from src.agents.po.tools_stories import reopen_story

        names = {tool.name for tool in get_all_tools()}
        named = set(re.findall(r"`([a-z_]+)", " ".join(self._rows().values())))
        assert named == {"get_product_situation", "list_stories", "reopen_story", "user_report"}
        assert named - {"user_report"} <= names
        assert "user_report" in reopen_story.args

    def test_an_event_is_told_by_the_snapshot_dates_not_as_a_fresh_incident(self):
        from src.agents.po.situation import SNAPSHOT_HEADING

        table = " ".join(_section("## Situation → Tool").split())
        assert '"Situation snapshot" built by code' in table
        assert "tell an old event by its dates, never present it as a fresh incident" in table
        assert SNAPSHOT_HEADING == "## Situation snapshot"


def test_deferred_notices_are_explicit_and_do_not_change_facts():
    section = _section("## Deferred Notices")
    for instruction in (
        "own judgement",
        "user's or an admin's word",
        "suppress_owner_notice",
        "reason",
        "Never drop a notice silently",
        "Deferring never changes the facts",
        "When the user returns",
        "tell deferred notices first",
        "resolve_deferred_notice",
        'outcome="told"',
        'outcome="closed"',
        "Nothing expires",
    ):
        assert instruction in section
