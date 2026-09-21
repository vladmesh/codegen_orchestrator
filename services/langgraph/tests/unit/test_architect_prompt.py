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


class TestUsageExampleDirectives:
    """The architect plans exactly the uses the user confirmed in the brief."""

    @staticmethod
    def _prompt() -> str:
        return " ".join(SYSTEM_PROMPT.split())

    def test_every_usage_example_becomes_one_qa_criterion_naming_its_requirement(self):
        prompt = self._prompt()
        assert "Turn every usage example of a requirement you plan into its own" in prompt
        assert "stated through what QA can do" in prompt
        assert "`(requirement <id>)`" in prompt
        assert (
            '- Telegram: sending "кофе 250" replies that an expense was recorded '
            "(requirement expense-text)"
        ) in SYSTEM_PROMPT

    def test_an_upload_example_is_checked_through_its_observable_or_marked(self):
        prompt = self._prompt()
        assert "is never silently dropped" in prompt
        assert "check it through its observable after the fact" in prompt
        assert "`not QA-verifiable: needs a photo upload`" in prompt
        assert "Never write the upload itself as a step." in prompt

    def test_a_requirement_with_an_undefined_input_is_returned_not_narrowed(self):
        prompt = self._prompt()
        assert "Return an undefined input; never narrow it." in prompt
        assert "do not plan the version the examples happen to show" in prompt
        assert "`record_requirement_coverage(requirement_id=..., returned_reason=...)`" in prompt
        assert "make the reason name the undefined input" in prompt

    def test_an_input_the_brief_settles_is_planned_not_returned(self):
        prompt = self._prompt()
        assert "A usage example or a limitation that settles the form" in prompt
        assert "planned as settled and not returned" in prompt

    def test_every_task_requires_asking_back_instead_of_storing_another_record_kind(self):
        prompt = self._prompt()
        assert "Every task for a product that accepts user input states this rule" in prompt
        assert "in its description and its acceptance criteria" in prompt
        assert "never stores input it does not recognize as a different kind of record" in prompt
        assert "it asks the user back what they meant" in prompt

    def test_accumulated_state_is_judged_from_a_starting_value_qa_reads_first(self):
        prompt = self._prompt()
        assert "Judge accumulated state from the value QA reads first." in prompt
        assert "a balance, a total, a count, a list of records" in prompt
        assert "is written relative to a starting value QA reads first" in prompt
        assert "in the example's reply wording" in prompt
        assert (
            '- Telegram: send "/balance" and note the starting balance, send "/income 5000" '
            'and "кофе 300"; "/balance" then replies "Баланс: <start + 4700> ₽" '
            "(requirement balance)"
        ) in SYSTEM_PROMPT

    def test_an_example_qa_cannot_reach_is_rewritten_or_returned_never_written(self):
        prompt = self._prompt()
        assert "**QA is one identity that cannot start over.**" in prompt
        assert (
            "Central QA acts as a single fixed Telegram identity whose product state "
            "persists across rounds and across stories, and it cannot reset that state, "
            "replace it, or act as a second user."
        ) in prompt
        assert (
            "An example that can only be observed from a state that identity cannot be in "
            '— a fresh user, an empty history, "no operations yet", another calendar month '
            "— is not a check"
        ) in prompt
        assert "**Rewrite it into what QA can observe.**" in prompt
        assert "becomes a check relative to the value QA reads first" in prompt
        assert "one criterion stands for both" in prompt
        assert (
            "A precondition that is merely a time the identity cannot occupy — another "
            "calendar month, a past period — has no rewrite and is not one."
        ) in prompt
        assert "**Return the requirement to the user**" in prompt
        assert "`record_requirement_coverage(requirement_id=..., returned_reason=...)`" in prompt
        assert "make the reason name the unreachable precondition" in prompt

    def test_a_stateless_reply_keeps_its_exact_wording(self):
        prompt = self._prompt()
        assert "A reply that does not depend on earlier records keeps its exact wording" in prompt
        assert "Keep the user's words: QA sends the message the example shows" in prompt

    def test_the_workflow_points_the_criteria_step_at_the_usage_examples(self):
        prompt = self._prompt()
        step = prompt[prompt.find("7. Call `update_acceptance_criteria`") :]
        step = step[: step.find("8. ")]
        assert 'see "Usage Examples" below' in step


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

    def test_requires_the_deployable_settings_contract_the_platform_calls(self):
        assert "POST /settings/set" in SYSTEM_PROMPT
        assert "POST /settings/get" in SYSTEM_PROMPT
        assert "generated settings registry" in SYSTEM_PROMPT.lower()
        assert "settings_schema" in SYSTEM_PROMPT
        assert "settings_schemas.py" in SYSTEM_PROMPT

    def test_package_owned_seed_replaces_product_owned_setting_glue(self):
        lower = SYSTEM_PROMPT.lower()

        assert "package-owned" in lower
        assert "declares its setting seed" in lower
        assert "db trigger" in lower
        assert "startup polling" in lower
        assert "product-owned seed code" in lower
        assert "duplicate" in lower and "service" in lower


class TestCriteriaUseOnlyTheQAVocabulary:
    """A criterion is stated through what QA can do, in the plan and in the brief."""

    @staticmethod
    def _brief_guidance() -> str:
        from src.agents.po.tools_briefs import present_product_brief

        return " ".join(present_product_brief.description.split())

    def test_the_architect_prompt_no_longer_invites_curl(self):
        assert "curl" not in SYSTEM_PROMPT.lower()

    def test_the_architect_prompt_names_every_qa_capable_action(self):
        prompt = " ".join(SYSTEM_PROMPT.split())
        assert "a read-only HTTP GET of a route" in prompt
        assert "a Telegram text message sent to the bot, and the bot's reply" in prompt
        assert "a press of an inline button" in prompt
        assert "a declared `FIRE JOB <name> ... THEN <observable>`" in prompt

    def test_the_architect_prompt_verifies_a_write_or_upload_through_its_observable(self):
        prompt = " ".join(SYSTEM_PROMPT.split())
        assert "never uploads a photo, file or other media" in prompt
        assert "verified through its observable after the fact" in prompt
        assert "never as a POST or an upload step" in prompt

    def test_the_brief_guidance_carries_the_same_rule(self):
        guidance = self._brief_guidance()
        assert "a read-only HTTP GET" in guidance
        assert "a Telegram text message and its reply" in guidance
        assert "an inline button press" in guidance
        assert "never as a POST or an upload step" in guidance


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

    def test_requires_a_provider_that_is_live_in_the_deployed_topology(self):
        lower = SYSTEM_PROMPT.lower()
        assert "deployed topology" in lower
        assert "services.yml" in SYSTEM_PROMPT
        assert "compose.base.yml" in SYSTEM_PROMPT
        assert "compose.prod.yml" in SYSTEM_PROMPT
        assert "durable output" in lower
        assert "dispatch_status" in SYSTEM_PROMPT

    def test_prefers_the_existing_worker_and_makes_a_new_provider_fully_deployable(self):
        assert "notifications_worker" in SYSTEM_PROMPT
        assert "Dockerfile" in SYSTEM_PROMPT
        assert "env.contract.yaml" in SYSTEM_PROMPT
        assert "CI build/push matrix" in SYSTEM_PROMPT

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

    def test_requires_a_black_box_observable_and_a_real_provider_path_test(self):
        prompt = " ".join(SYSTEM_PROMPT.lower().split())
        assert "read-only black-box observable" in prompt
        assert "seed the confirmed setting values" in prompt
        assert "fire the real named job contract" in prompt
        assert "exactly one durable record for each configured output partition" in prompt

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


class TestCapabilityShapeDirectives:
    """Where a capability lives is the architect's decision, and a package has a protocol."""

    def test_states_the_ladder_in_order(self):
        prompt = " ".join(SYSTEM_PROMPT.split())
        order = [
            "**Reuse what exists.**",
            "**A shared service.**",
            "**A container.**",
            "**An in-process kit package.**",
        ]
        positions = [prompt.find(rung) for rung in order]
        assert all(position > 0 for position in positions), positions
        assert positions == sorted(positions), positions

    def test_names_the_two_shapes_that_disqualify_a_package(self):
        prompt = " ".join(SYSTEM_PROMPT.lower().split())
        assert "a capability that needs a synchronous call into the host does not fit" in prompt
        assert "a capability that needs a stateless consumer does not fit" in prompt

    def test_names_the_protocol_constraints_a_package_plan_accepts(self):
        prompt = " ".join(SYSTEM_PROMPT.lower().split())
        assert "the only supported outward dependency is the event bus" in prompt
        assert "prefixed settings and job names" in prompt
        assert "owns its own postgres schema" in prompt
        assert "package boundaries are import boundaries" in prompt
        assert "only `in_process` is implemented" in prompt

    def test_the_package_task_installs_and_never_hand_writes_package_code(self):
        prompt = " ".join(SYSTEM_PROMPT.split())
        assert "Package code is never hand-written into a product." in prompt
        assert "`kit add <name> --wheel <path>`" in prompt
        assert "regeneration" in prompt.lower()

    def test_points_at_the_recipe_instead_of_restating_it(self):
        prompt = " ".join(SYSTEM_PROMPT.split())
        assert "docs/CONTRACTS.md" in prompt
        assert "Installing a kit package into a generated product" in prompt

    def test_leaves_a_story_that_needs_no_new_capability_untouched(self):
        prompt = " ".join(SYSTEM_PROMPT.lower().split())
        assert "a story whose capability already exists gets none of this" in prompt


class TestDecompositionPhilosophyIsReconciled:
    """The reader is told which decision is the architect's and which the developer's."""

    def test_says_shape_is_the_architects_and_implementation_the_developers(self):
        prompt = " ".join(SYSTEM_PROMPT.split())
        assert "Shape is yours; implementation inside it is the developer's." in prompt

    def test_the_over_specification_rule_excepts_the_capability_shape(self):
        prompt = " ".join(SYSTEM_PROMPT.split())
        rule = "Do NOT over-specify implementation details"
        assert rule in prompt
        tail = prompt[prompt.find(rule) : prompt.find(rule) + 400]
        assert "Naming the capability shape is not over-specification" in tail
