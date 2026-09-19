"""Offline contracts for the level-1 confirmed brief and its model-free admission.

Everything here is checkable without a stand, and each test asks the question a
stand run would otherwise be the first to ask:

* is the document the harness presents one the *released* write shape accepts,
  and is the setting it confirms one the product this run deploys can hold;
* does the admission drive the architect's own routes in the order that crosses
  the coverage gate, rather than stepping around it;
* is the message a bot owner gets the one a bot product sends — and is a message
  carrying a backend address refused, which is the whole point of the change.
"""

from __future__ import annotations

import json

import httpx
from jsonschema import Draft202012Validator
from level1_brief import (
    LEVEL1_BRIEF_LANGUAGE,
    LEVEL1_COMMAND_REQUIREMENT,
    LEVEL1_SETTING_REQUIREMENT,
    bot_completion_message_mismatches,
    build_level1_brief,
)
from level1_change_set import LEVEL1_SETTING_KEY, backend_operations
import pipeline_helpers
import pytest
import yaml

from shared.contracts.dto.product_brief import ProposedProductBriefContent

pytestmark = pytest.mark.needs_no_api_credential

MARKER = "e2e-abc123def456"


def _client(handler):
    return httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler))


def _proposed_content(brief) -> ProposedProductBriefContent:
    """The document as the released write boundary parses it, not as a dict."""
    arguments = brief.present_arguments("11111111-1111-1111-1111-111111111111")
    return ProposedProductBriefContent(
        summary=arguments["summary"],
        must_requirements=arguments["must_requirements"],
        language=arguments["language"],
        usage_examples=arguments["usage_examples"],
        limitations=arguments["limitations"],
        initial_settings=arguments["initial_settings"],
    )


def test_the_level1_brief_is_a_document_the_released_write_shape_accepts():
    """It parses as a proposed revision, so `present_product_brief` cannot refuse it.

    The write shape is where "a user-facing requirement nobody showed the user
    how to use" and "a brief with no language" are refused, so parsing here is
    the same check the live tool performs — asserted through the contract rather
    than re-stated as a list of expected strings.
    """
    content = _proposed_content(build_level1_brief(MARKER))

    assert content.language == LEVEL1_BRIEF_LANGUAGE
    assert content.limitations
    exemplified = {example.requirement_id for example in content.usage_examples}
    user_facing = {
        requirement.id for requirement in content.must_requirements if requirement.user_facing
    }
    assert user_facing == {LEVEL1_COMMAND_REQUIREMENT, LEVEL1_SETTING_REQUIREMENT}
    assert user_facing <= exemplified
    assert [(setting.key, setting.scope.value) for setting in content.initial_settings] == [
        (LEVEL1_SETTING_KEY, "product")
    ]
    assert content.initial_settings[0].description


def test_the_confirmed_setting_is_one_the_deployed_level1_product_can_hold():
    """The key the user confirms is the key the change set declares, and the value fits.

    The deploy writes the confirmed value through the product's own
    `settings.set`, which refuses a key its manifest does not declare and a
    value its schema does not admit. Both facts live in this repository — the
    brief here and the manifest the change set writes — so disagreeing about
    them is a unit-test failure rather than a seed failure an hour into a run.
    """
    brief = build_level1_brief(MARKER)
    manifest = next(
        operation for operation in backend_operations(MARKER) if operation.op == "replace"
    )
    declared = yaml.safe_load(manifest.content)["settings_schema"]["properties"]

    assert brief.settings_key in declared
    schema = declared[brief.settings_key]
    assert not list(Draft202012Validator(schema).iter_errors(brief.settings_value))
    # And it is not what an unseeded product would declare for itself: a
    # readback equal to the manifest default would prove nothing.
    assert brief.settings_value != schema["default"]


def test_the_completion_message_of_a_bot_product_is_accepted():
    """The shape `_telegram_bot_usage_instructions` composes passes unchanged."""
    brief = build_level1_brief(MARKER)
    examples = [dict(example) for example in brief.usage_examples]
    text = (
        "The story is finished: it is deployed and QA passed. Tell the user the good news "
        "in the brief's language (ru) and how to use it: they reach the Telegram bot "
        "@mega_e2e_codegen_bot (https://t.me/mega_e2e_codegen_bot). Usage examples from "
        "the confirmed brief (what the user sends and what the bot answers):\n"
        + "\n".join(
            f"- the user sends: {one['user_sends']} → the bot answers: {one['product_answers']}"
            for one in examples
        )
        + "\nDo not give the user any server, API or backend address."
    )

    assert (
        bot_completion_message_mismatches(
            text,
            bot_username="mega_e2e_codegen_bot",
            usage_examples=examples,
            language="ru",
        )
        == []
    )


def test_a_completion_message_that_gives_a_backend_address_is_refused():
    """The message the harness used to demand is exactly the one now refused.

    A bot owner has no use for the backend address and the Definition of Done
    says they must not be given one, so a message carrying it fails here — and
    a message carrying some *other* server address fails too, because the claim
    is "every link is the bot's", not "this run's URL is absent".
    """
    brief = build_level1_brief(MARKER)
    examples = [dict(example) for example in brief.usage_examples]
    usable = (
        "The story is finished: it is deployed and QA passed. They reach the Telegram bot "
        "@mega_e2e_codegen_bot (https://t.me/mega_e2e_codegen_bot), brief's language (ru).\n"
        + "\n".join(
            f"- the user sends: {one['user_sends']} → the bot answers: {one['product_answers']}"
            for one in examples
        )
    )
    with_address = f"{usable}\nAddress: http://198.51.100.2:8010"

    assert (
        bot_completion_message_mismatches(
            usable,
            bot_username="mega_e2e_codegen_bot",
            usage_examples=examples,
            language="ru",
        )
        == []
    )
    reasons = bot_completion_message_mismatches(
        with_address,
        bot_username="mega_e2e_codegen_bot",
        usage_examples=examples,
        language="ru",
    )
    assert reasons == [
        "it gives the user an address that is not the bot's: ['http://198.51.100.2:8010']"
    ]


def test_a_message_missing_the_bot_language_or_an_example_says_which():
    """Every unmet claim is reported, so a red wait names all of them at once."""
    reasons = bot_completion_message_mismatches(
        "The story is finished.",
        bot_username="mega_e2e_codegen_bot",
        usage_examples=[
            {"requirement_id": "r1", "user_sends": "команду /level1", "product_answers": "маркер"}
        ],
        language="ru",
    )

    assert reasons == [
        "it does not name the bot @mega_e2e_codegen_bot",
        "it does not name the brief's language (ru)",
        "it does not carry the confirmed usage example r1.user_sends: 'команду /level1'",
        "it does not carry the confirmed usage example r1.product_answers: 'маркер'",
    ]


def _admission_context() -> dict:
    brief = build_level1_brief(MARKER)
    return {
        "project_id": "11111111-1111-1111-1111-111111111111",
        "story_id": "story-1",
        "brief_id": "brief-abc",
        "level1_brief": brief,
        "level1_planning_attempt_id": ATTEMPT,
        "task_title": "backend task",
        "task_description": "backend change set",
        "followup_task_title": "bot task",
        "followup_task_description": "bot change set",
    }


ATTEMPT = "plan-deadbeef"


def _admission_handler(  # noqa: C901 - one fake of the whole planning surface
    calls: list[tuple[str, str]],
    *,
    dispatch_admitted_before: bool = False,
    admission_outcome: str = "admitted",
    story_status: str = "created",
    start_status: int = 200,
    brief_attempt_id: str = ATTEMPT,
    brief_attempt_active: bool | None = None,
    extra_tasks: list[dict] | None = None,
):
    """One fake of every route the admission drives, with the levers a test needs."""
    tasks: list[dict] = []
    coverage: list[dict] = []
    story = {"id": "story-1", "status": story_status}

    def brief_row() -> dict:
        admitted = any(task["dispatch_admitted"] for task in tasks) and tasks
        active = brief_attempt_active if brief_attempt_active is not None else not bool(admitted)
        return {
            "id": "brief-abc",
            "story_id": "story-1",
            "planning_attempt_id": brief_attempt_id,
            "planning_attempt_active": active,
            "coverage_admitted_at": "2026-09-19T10:00:00Z" if admitted else None,
        }

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 - one route each
        path = request.url.path
        calls.append((request.method, path))
        if request.method == "GET" and path == "/api/stories/story-1":
            return httpx.Response(200, json=story)
        if request.method == "POST" and path == "/api/stories/story-1/start":
            if start_status == 200:
                story["status"] = "in_progress"
                return httpx.Response(200, json=story)
            return httpx.Response(start_status, json={"detail": "Cannot transition"})
        if request.method == "POST" and path == "/api/tasks/":
            body = json.loads(request.content)
            task = {
                "id": f"task-{len(tasks) + 1}",
                "story_id": "story-1",
                "status": body["status"],
                "dispatch_admitted": dispatch_admitted_before,
                "planning_attempt_id": body["planning_attempt_id"],
                "blocked_by_task_id": body["blocked_by_task_id"],
                "created_by": body["created_by"],
            }
            tasks.append(task)
            return httpx.Response(201, json=task)
        if request.method == "GET" and path == "/api/tasks/":
            return httpx.Response(200, json=[*tasks, *(extra_tasks or [])])
        if request.method == "GET" and path.startswith("/api/tasks/"):
            task = next(one for one in tasks if one["id"] == path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=task)
        if request.method == "PUT" and "/coverage/" in path:
            body = json.loads(request.content)
            coverage.append({**body, "brief_id": "brief-abc", "returned_reason": None})
            return httpx.Response(200, json=coverage[-1])
        if request.method == "POST" and path == "/api/product-briefs/brief-abc/admit":
            if admission_outcome == "admitted":
                for task in tasks:
                    task["dispatch_admitted"] = True
            return httpx.Response(
                200,
                json={
                    "brief_id": "brief-abc",
                    "story_id": "story-1",
                    "outcome": admission_outcome,
                    "coverage_admitted_at": "2026-09-19T10:00:00Z",
                    "released_task_ids": [task["id"] for task in tasks],
                    "missing_requirement_ids": [],
                },
            )
        if request.method == "GET" and path == "/api/product-briefs/brief-abc":
            return httpx.Response(200, json=brief_row())
        if request.method == "GET" and path == "/api/product-briefs/brief-abc/coverage":
            return httpx.Response(200, json=coverage)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return handler


@pytest.mark.asyncio
async def test_the_plan_is_built_unadmitted_and_released_only_by_the_admission():
    """Claim → tasks under the attempt → one disposition each → the one admission.

    The order is the contract: the tasks are created undispatchable under this
    run's planning attempt, every must-requirement gets exactly one disposition
    naming the task that covers it, and nothing is dispatchable until the
    admission step says so. Nothing here asks a model anything.
    """
    ctx = _admission_context()
    calls: list[tuple[str, str]] = []

    async with _client(_admission_handler(calls)) as api:
        await pipeline_helpers.admit_level1_plan(api, ctx)

    assert ctx["task_ids"] == ["task-1", "task-2"]
    assert ctx["first_task_id"] == ctx["task_id"] == "task-1"
    assert ctx["second_task_id"] == "task-2"
    assert [task["dispatch_admitted"] for task in ctx["level1_plan_before_admission"]] == [
        False,
        False,
    ]
    assert [task["dispatch_admitted"] for task in ctx["level1_plan_after_admission"]] == [
        True,
        True,
    ]
    assert ctx["level1_admission"]["released_task_ids"] == ["task-1", "task-2"]
    assert {row["requirement_id"]: row["task_id"] for row in ctx["level1_coverage"]} == {
        LEVEL1_SETTING_REQUIREMENT: "task-1",
        LEVEL1_COMMAND_REQUIREMENT: "task-2",
    }
    assert all(row["planning_attempt_id"] == ATTEMPT for row in ctx["level1_coverage"])
    # Every disposition is recorded before the admission, and the admission is
    # reached exactly once: a plan admitted mid-way releases work nothing covers.
    coverage_calls = [index for index, call in enumerate(calls) if call[0] == "PUT"]
    admit_call = calls.index(("POST", "/api/product-briefs/brief-abc/admit"))
    assert coverage_calls and max(coverage_calls) < admit_call
    assert calls.count(("POST", "/api/product-briefs/brief-abc/admit")) == 1


@pytest.mark.asyncio
async def test_a_plan_whose_tasks_were_dispatchable_early_stops_naming_the_phase():
    """The gate is read, not assumed: a released task before admission is a defect."""
    ctx = _admission_context()

    async with _client(_admission_handler([], dispatch_admitted_before=True)) as api:
        with pytest.raises(pipeline_helpers.Level1PhaseFailed) as refused:
            await pipeline_helpers.admit_level1_plan(api, ctx)

    assert refused.value.phase == "admission"
    assert "dispatchable before" in refused.value.reason


@pytest.mark.asyncio
async def test_an_admission_that_did_not_admit_stops_naming_the_phase():
    """`incomplete` releases nothing, so the run says so instead of waiting on tasks."""
    ctx = _admission_context()

    async with _client(_admission_handler([], admission_outcome="incomplete")) as api:
        with pytest.raises(pipeline_helpers.Level1PhaseFailed) as refused:
            await pipeline_helpers.admit_level1_plan(api, ctx)

    assert refused.value.phase == "admission"
    assert "incomplete" in refused.value.reason


def _unbound_refusal() -> httpx.Response:
    """The API's own answer for a brief that has no story to plan in yet."""
    return httpx.Response(422, json={"detail": pipeline_helpers.PLAN_HAS_NO_STORY_DETAIL})


@pytest.mark.asyncio
async def test_the_claim_is_retried_through_the_pre_bind_refusal_with_no_delay():
    """The claim itself is the loop: no read of the brief, and no sleep in the window.

    `create_story` binds the brief and only then publishes the story to the
    architect, so the claim has to land in the gap between those two. Retrying
    the claim through its own 422 removes the extra read and the poll interval
    that used to sit in that gap.
    """
    ctx = {"brief_id": "brief-abc"}
    calls: list[str] = []
    answers = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        answers["count"] += 1
        if answers["count"] < 3:
            return _unbound_refusal()
        return httpx.Response(200, json={"outcome": "claimed", "planning_attempt_id": ATTEMPT})

    async with _client(handler) as api:
        claim = await pipeline_helpers._claim_plan_as_soon_as_it_is_claimable(api, ctx)

    assert claim == {"outcome": "claimed", "planning_attempt_id": ATTEMPT}
    assert ctx["level1_plan_claim_attempts"] == 3
    assert calls == ["POST /api/product-briefs/brief-abc/planning-attempts/claim"] * 3


@pytest.mark.asyncio
async def test_a_claim_refused_for_any_other_reason_stops_naming_the_phase():
    """Only "no story yet" is retried; anything else is an answer, not a wait."""
    ctx = {"brief_id": "brief-abc"}

    async with _client(lambda _request: httpx.Response(409, text="gone")) as api:
        with pytest.raises(pipeline_helpers.Level1PhaseFailed) as refused:
            await pipeline_helpers._claim_plan_as_soon_as_it_is_claimable(api, ctx)

    assert refused.value.phase == "brief"
    assert "409" in refused.value.reason


@pytest.mark.asyncio
async def test_a_story_the_architect_already_started_is_not_an_error():
    """The architect starts this story too, and whoever got there first is right.

    `consumers/architect.py` transitions the story with `start` before the claim
    that turns it away, and `IN_PROGRESS -> IN_PROGRESS` is not a declared
    transition — so an unconditional second `start` would turn a lost race into
    a raw HTTP error. The story's landing place is what this run needs.
    """
    ctx = _admission_context()
    calls: list[tuple[str, str]] = []

    async with _client(_admission_handler(calls, story_status="in_progress")) as api:
        await pipeline_helpers.admit_level1_plan(api, ctx)

    assert ctx["level1_story_started_by"] == "architect"
    assert ("POST", "/api/stories/story-1/start") not in calls


@pytest.mark.asyncio
async def test_a_start_lost_between_the_read_and_the_post_is_not_an_error():
    """The architect can start it in the gap; the re-read is what decides."""
    ctx = _admission_context()
    calls: list[tuple[str, str]] = []
    states = {"started": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/stories/story-1":
            calls.append((request.method, path))
            status = "in_progress" if states["started"] else "created"
            return httpx.Response(200, json={"id": "story-1", "status": status})
        if request.method == "POST" and path == "/api/stories/story-1/start":
            calls.append((request.method, path))
            # Somebody else got there between the read above and this post.
            states["started"] = True
            return httpx.Response(422, json={"detail": "Cannot transition"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with _client(handler) as api:
        landed = await pipeline_helpers._start_level1_story(api, ctx)

    assert landed == "in_progress"
    assert ctx["level1_story_started_by"] == "architect"
    assert calls == [
        ("GET", "/api/stories/story-1"),
        ("POST", "/api/stories/story-1/start"),
        ("GET", "/api/stories/story-1"),
    ]


@pytest.mark.asyncio
async def test_a_story_that_did_not_reach_in_progress_stops_naming_the_phase():
    """A refusal that is not a lost race is still a failure, and it names its phase."""
    ctx = _admission_context()

    async with _client(_admission_handler([], story_status="created", start_status=409)) as api:
        with pytest.raises(pipeline_helpers.Level1PhaseFailed) as refused:
            await pipeline_helpers._start_level1_story(api, ctx)

    assert refused.value.phase == "admission"
    assert "could not be started" in refused.value.reason


@pytest.mark.asyncio
async def test_a_plan_claimed_by_something_else_is_caught_from_durable_state():
    """A rival claim mints a new attempt id, and that is what this reads.

    This is the zero-model property proved rather than assumed: an architect
    that took the plan over would be planning this story with its model, and the
    only trace the harness needs is the attempt id on the brief no longer being
    the one this run claimed.
    """
    ctx = _admission_context()

    async with _client(_admission_handler([], brief_attempt_id="plan-somebody-else")) as api:
        with pytest.raises(pipeline_helpers.Level1PhaseFailed) as refused:
            await pipeline_helpers.admit_level1_plan(api, ctx)

    assert refused.value.phase == "admission"
    assert "something else claimed this plan" in refused.value.reason


@pytest.mark.asyncio
async def test_a_claim_finished_out_from_under_the_run_is_caught():
    """An unadmitted plan whose attempt is closed was given up by somebody."""
    ctx = _admission_context()

    async with _client(_admission_handler([], brief_attempt_active=False)) as api:
        with pytest.raises(pipeline_helpers.Level1PhaseFailed) as refused:
            await pipeline_helpers.admit_level1_plan(api, ctx)

    assert refused.value.phase == "admission"
    assert "no longer active" in refused.value.reason


@pytest.mark.asyncio
async def test_a_task_this_run_never_planned_in_the_story_is_caught():
    """An architect that did run its graph would leave its own tasks here."""
    ctx = _admission_context()
    foreign = {
        "id": "task-architect",
        "story_id": "story-1",
        "status": "todo",
        "dispatch_admitted": True,
        "planning_attempt_id": "plan-somebody-else",
        "created_by": "architect",
    }

    async with _client(_admission_handler([], extra_tasks=[foreign])) as api:
        with pytest.raises(pipeline_helpers.Level1PhaseFailed) as refused:
            await pipeline_helpers.admit_level1_plan(api, ctx)

    assert refused.value.phase == "admission"
    assert "tasks this run did not plan" in refused.value.reason


@pytest.mark.asyncio
async def test_the_provenance_observations_are_kept_in_order_for_the_artifact():
    """Every observation is retained, so a green run can be read back afterwards."""
    ctx = _admission_context()

    async with _client(_admission_handler([])) as api:
        await pipeline_helpers.admit_level1_plan(api, ctx)
        await pipeline_helpers.verify_level1_plan_is_this_runs_alone(
            api, ctx, when="after_engineering"
        )

    assert [one["when"] for one in ctx["level1_plan_provenance"]] == [
        "before_admission",
        "after_admission",
        "after_engineering",
    ]
    assert {one["planning_attempt_id"] for one in ctx["level1_plan_provenance"]} == {ATTEMPT}
    assert [one["planning_attempt_active"] for one in ctx["level1_plan_provenance"]] == [
        True,
        False,
        False,
    ]
    assert all(one["task_ids"] == ["task-1", "task-2"] for one in ctx["level1_plan_provenance"])


def test_the_seed_line_is_selected_by_this_runs_deploy_and_names_its_route(monkeypatch):
    """The deploy's own statement of which brief it read, for this run's deploy only."""
    other = json.dumps(
        {"event": "deploy_settings_seed_brief", "task_id": "deploy-other", "route": "project"}
    )
    mine = json.dumps(
        {
            "event": "deploy_settings_seed_brief",
            "task_id": "deploy-grant-1",
            "brief_id": "brief-abc",
            "route": "story",
            "settings_count": 1,
        }
    )

    class _Result:
        returncode = 0
        stdout = f"deploy-worker-1  | {other}\ndeploy-worker-1  | {mine}\nnot json at all\n"

    monkeypatch.setattr(
        pipeline_helpers.subprocess,
        "run",
        lambda *args, **kwargs: _Result(),  # noqa: ARG005
    )
    ctx = {"deploy_run_id": "deploy-grant-1"}

    pipeline_helpers.record_settings_seed_brief_log(ctx)

    assert ctx["settings_seed_brief_log_error"] is None
    assert ctx["settings_seed_brief_log"] == {
        "event": "deploy_settings_seed_brief",
        "task_id": "deploy-grant-1",
        "brief_id": "brief-abc",
        "route": "story",
        "settings_count": 1,
    }


def test_a_seed_line_this_run_never_wrote_is_a_stated_reason(monkeypatch):
    """An absent line is reported, never silently read as somebody else's deploy."""

    class _Result:
        returncode = 0
        stdout = "deploy-worker-1  | " + json.dumps(
            {"event": "deploy_settings_seed_brief", "task_id": "deploy-other", "route": "project"}
        )

    monkeypatch.setattr(
        pipeline_helpers.subprocess,
        "run",
        lambda *args, **kwargs: _Result(),  # noqa: ARG005
    )
    ctx = {"deploy_run_id": "deploy-grant-1"}

    pipeline_helpers.record_settings_seed_brief_log(ctx)

    assert "settings_seed_brief_log" not in ctx
    assert "deploy-grant-1" in ctx["settings_seed_brief_log_error"]
