"""Unit tests for architect system prompt — ensures key directives are present."""

from __future__ import annotations

from shared.contracts.acceptance import parse_scheduled_behaviours
from src.prompts.architect import SYSTEM_PROMPT


class TestArchitectPromptContent:
    def test_references_scaffolded_project(self):
        assert "scaffold" in SYSTEM_PROMPT.lower()

    def test_references_agents_md(self):
        assert "AGENTS.md" in SYSTEM_PROMPT

    def test_prohibits_infrastructure_tasks(self):
        lower = SYSTEM_PROMPT.lower()
        assert "do not create tasks for" in lower or "do not create" in lower
        assert "infrastructure" in lower or "docker" in lower or "ci" in lower

    def test_mentions_business_logic_focus(self):
        assert "business logic" in SYSTEM_PROMPT.lower()

    def test_mentions_diff(self):
        lower = SYSTEM_PROMPT.lower()
        assert "diff" in lower or "difference" in lower

    def test_mentions_task_count_guidance(self):
        assert "1" in SYSTEM_PROMPT and "2" in SYSTEM_PROMPT


class TestProductBriefDirectives:
    """The prompt has to say what releases a brief-backed plan, and how."""

    def test_names_the_tool_that_records_a_disposition(self):
        assert "record_requirement_coverage" in SYSTEM_PROMPT

    def test_demands_exactly_one_disposition_per_requirement(self):
        lower = SYSTEM_PROMPT.lower()
        assert "must-requirement" in lower
        assert "exactly one disposition" in lower
        assert "one call per requirement" in lower

    def test_says_nothing_is_dispatched_until_all_of_them_are_recorded(self):
        assert "Nothing you planned is dispatched until all of them are recorded" in SYSTEM_PROMPT


class TestInitialSettingsDirectives:
    """A confirmed setting is planned for, not written by the plan."""

    def test_requires_the_manifest_declaration_that_makes_a_key_writable(self):
        lower = SYSTEM_PROMPT.lower()
        assert "settings_schema" in lower
        assert "manifest.yaml" in lower

    def test_says_the_platform_writes_the_values_and_the_plan_does_not(self):
        lower = SYSTEM_PROMPT.lower()
        assert "the platform writes them" in lower
        assert "setting key not declared" in lower


class TestScheduledBehaviourDirectives:
    """A behaviour the product runs on a schedule is declared, provided and fired."""

    def test_requires_the_manifest_declaration_that_makes_a_name_fireable(self):
        lower = SYSTEM_PROMPT.lower()
        assert "jobs_schema" in lower
        assert "manifest.yaml" in lower
        assert "additionalproperties: false" in lower
        assert "job name not declared" in lower

    def test_says_the_core_schedules_nothing_and_the_plan_owes_a_provider(self):
        lower = SYSTEM_PROMPT.lower()
        assert "the core schedules nothing" in lower
        assert 'provides: ["jobs.fire"]' in SYSTEM_PROMPT
        assert "subscribes to `job_fired`" in SYSTEM_PROMPT

    def test_teaches_the_criterion_form_qa_fires_from(self):
        assert '- FIRE JOB <name> WITH {"json": "arguments"} THEN <observable>' in SYSTEM_PROMPT

    def test_demands_the_declared_name_verbatim(self):
        lower = SYSTEM_PROMPT.lower()
        assert "character for character the string the manifest declares" in lower
        assert "not a paraphrase" in lower

    def test_takes_the_observable_from_the_typed_settings(self):
        assert 'settings.languages = ["ru", "en"]' in SYSTEM_PROMPT
        lower = SYSTEM_PROMPT.lower()
        assert "each configured language" in lower
        assert "never from a list re-derived from the story description" in lower

    def test_demands_a_capability_and_refuses_a_sample(self):
        lower = SYSTEM_PROMPT.lower()
        assert "assert a capability, not a sample" in lower
        assert "there is a russian item this week" in lower
        assert "false" in lower and "red" in lower

    def test_invents_no_behaviour_where_the_brief_asked_for_none(self):
        lower = SYSTEM_PROMPT.lower()
        assert "a story with no scheduled behaviour gets no `fire job` line" in lower
        assert "invents a behaviour the brief did not ask for" in lower


class TestPromptCriterionFormRoundTrips:
    """What the prompt teaches is what the released parser reads — one pattern, not two."""

    def test_every_worked_criterion_line_is_read_by_the_released_parser(self):
        worked = [
            line.strip()
            for line in SYSTEM_PROMPT.splitlines()
            if line.strip().startswith("- FIRE JOB") and "<" not in line
        ]

        assert worked, "the prompt shows no worked FIRE JOB line to round-trip"
        for line in worked:
            behaviours = parse_scheduled_behaviours(line)
            assert len(behaviours) == 1, line
            assert behaviours[0].name == "daily_digest"
            assert behaviours[0].arguments == {"languages": ["ru", "en"]}
            assert behaviours[0].observable == "a digest per configured language"
