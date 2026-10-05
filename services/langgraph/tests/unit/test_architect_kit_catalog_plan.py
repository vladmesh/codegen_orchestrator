"""A scripted architect resolves two briefs against the kit catalog through the real graph.

The model is scripted to make the calls an architect following the prompt and the
"Kit package catalog" block makes; everything past it is real: the consumer, its
briefing, the graph, its tool node, `create_task` with its package check, and the
admission. The catalog is the unit double (`kit_catalog_off_github` in `conftest.py`):
the pinned kit tooling's own catalog, filtered by the real `installable`. What is
asserted is what the tools sent to the stub API, with the checks the opt-in real-LLM run
(`tests/e2e/test_architect_kit_catalog_plan.py`) applies to a live model against the live
catalog. The counterfactual scripts show the checks fail on a plan that hand-builds the
capability, installs from an artifact, invents a package or installs one nothing asked for.
"""

from __future__ import annotations

from dataclasses import dataclass
import itertools
from unittest.mock import patch

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
import pytest

from src.agents.architect.tools import reset_task_chain
from src.kit_catalog import KitCatalogFailure, KitCatalogUnavailable
from tests.unit.architect_kit_catalog import (
    PREVIOUS_CRITERIA,
    SHOPPING_ADD,
    SHOPPING_LIST,
    CatalogBrief,
    CatalogPlanApi,
    assert_every_requirement_is_disposed,
    assert_plan_installs_no_package,
    assert_plan_installs_reminders_from_the_catalog,
    plan_story,
    reminders_brief,
    shopping_list_brief,
)
from tests.unit.conftest import BUNDLED_KIT_CATALOG_SOURCE
from tests.unit.test_architect_graph import _ScriptedToolCallingModel

RECIPE = (
    'Follow docs/contracts/kit-template-and-qa.md, "Installing a kit package into a generated '
    'product".'
)
INSTALL_CRITERIA = (
    "- services/backend/manifest.yaml lists reminders under packages\n"
    "- the regenerated package contract (codegen_kit/_active_packages.py) records reminders\n"
    '- Telegram: sending "/remind 2026-10-05 09:00 call the bank" replies '
    '"Reminder set for 2026-10-05 09:00: call the bank"'
)


class _RecordingScriptedModel(_ScriptedToolCallingModel):
    """The scripted model, keeping what it was asked so the briefing can be asserted."""

    seen: list[list[BaseMessage]] = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ANN003
        self.seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


@dataclass(frozen=True)
class _Task:
    title: str
    description: str
    acceptance_criteria: str


def _script(api: CatalogPlanApi, tasks: list[_Task], covers: list[str]) -> list[AIMessage]:
    """Create `tasks`, cover every id in `covers` with the first, record the criteria."""
    ids = (f"call-{n}" for n in itertools.count(1))

    def turn(*calls: tuple[str, dict]) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[{"name": name, "args": args, "id": next(ids)} for name, args in calls],
        )

    turns = [
        turn(
            (
                "plan_install" if task is _INSTALL_REMINDERS else "create_task",
                {"name": "reminders"}
                if task is _INSTALL_REMINDERS
                else {
                    "title": task.title,
                    "description": task.description,
                    "type": "feature",
                    "acceptance_criteria": task.acceptance_criteria,
                },
            )
        )
        for task in tasks
    ]
    turns.append(
        turn(
            *[
                ("record_requirement_coverage", {"requirement_id": rid, "task_id": "task-1"})
                for rid in covers
            ]
        )
    )
    criteria = "\n".join(
        [PREVIOUS_CRITERIA]
        + [
            f'- Telegram: sending "{example.user_sends}" replies "{example.product_answers}" '
            f"(requirement {example.requirement_id})"
            for example in api.content.usage_examples
        ]
    )
    update = {"project_id": str(api.brief.project_id), "acceptance_criteria": criteria}
    turns.append(turn(("update_acceptance_criteria", update)))
    turns.append(AIMessage(content="The plan is recorded."))
    return turns


async def _run(brief: CatalogBrief, tasks: list[_Task]) -> tuple[CatalogPlanApi, dict, list]:
    api = CatalogPlanApi(brief)
    covers = [requirement.id for requirement in brief.content.must_requirements]
    model = _RecordingScriptedModel(turns=_script(api, tasks, covers), seen=[])
    reset_task_chain()
    with patch("src.llm.openrouter.ChatOpenAI", return_value=model):
        result = await plan_story(api, model="scripted", base_url="http://llm.invalid", api_key="x")
    reset_task_chain()
    return api, result, model.seen


def _tool_errors(seen: list[list[BaseMessage]]) -> list[str]:
    return [
        str(message.content)
        for message in seen[-1]
        if isinstance(message, ToolMessage) and '"error"' in str(message.content)
    ]


_INSTALL_REMINDERS = _Task(
    "Install the reminders package and wire /remind",
    "The one-time reminder capability is the `reminders` kit package from the catalog: "
    f"install it with `kit add reminders` from the product root. {RECIPE} The bot's /remind "
    "command creates a reminder through the package's /reminders route for the verified "
    "caller.",
    INSTALL_CRITERIA,
)
_HAND_BUILT_REMINDERS = _Task(
    "Reminders in the backend",
    "Add a reminders table, a /remind command and a backend timer that sends due reminders.",
    "- GET /reminders lists a reminder created with /remind",
)
_SHOPPING_LIST = _Task(
    "Shopping list",
    "Add an items table in the backend with add and list endpoints, and /add and /list "
    "commands in the bot.",
    '- Telegram: sending "/add milk" replies "Added: milk"\n'
    '- Telegram: sending "/list" replies "Your list: milk"',
)


@pytest.mark.asyncio
async def test_a_reminders_brief_installs_the_catalog_package():
    api, result, seen = await _run(reminders_brief(), [_INSTALL_REMINDERS])

    assert result["status"] == "success", result
    assert api.released == ["task-1"]
    assert_every_requirement_is_disposed(api)
    assert_plan_installs_reminders_from_the_catalog(api)
    # The planner was shown the catalog the double read, package by package.
    briefing = seen[0][-1].content
    assert f"Kit package catalog (read live from {BUNDLED_KIT_CATALOG_SOURCE}" in briefing
    assert "- reminders (installs 0.5.0): One-time text reminders" in briefing
    assert "  capabilities: remind me at a time; schedule a one-time text reminder" in briefing
    assert "  settings it asks for: reminder_owner_ref: Owner of the seeded" in briefing
    assert "  required environment: REDIS_URL: Redis broker" in briefing
    assert "plan_install" in briefing
    assert "textparse" in briefing and "default binding" in briefing


@pytest.mark.asyncio
async def test_a_brief_no_package_covers_plans_no_package():
    api, result, seen = await _run(shopping_list_brief(), [_SHOPPING_LIST])

    assert result["status"] == "success", result
    assert api.released == ["task-1"]
    assert {SHOPPING_ADD, SHOPPING_LIST} == set(api.coverage)
    assert_plan_installs_no_package(api)
    assert "- reminders (installs 0.5.0)" in seen[0][-1].content


@pytest.mark.asyncio
async def test_an_unavailable_catalog_is_briefed_and_refuses_every_install(kit_catalog_off_github):
    kit_catalog_off_github.read.return_value = KitCatalogUnavailable(
        "https://kit.invalid/catalog.yaml", KitCatalogFailure.STATUS, "HTTP 503"
    )

    api, _, seen = await _run(reminders_brief(), [_INSTALL_REMINDERS])

    briefing = seen[0][-1].content
    assert "Kit package catalog: unavailable this time" in briefing
    assert "status, HTTP 503" in briefing
    assert "No kit package can be planned in this run" in briefing
    assert "- reminders (installs" not in briefing
    assert api.task_payloads == []
    (refusal,) = _tool_errors(seen)
    assert "catalog_unavailable" in refusal


def _with(task: _Task, description: str) -> _Task:
    return _Task(task.title, description, task.acceptance_criteria)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("brief", "task", "refused", "failure"),
    [
        pytest.param(
            reminders_brief,
            _HAND_BUILT_REMINDERS,
            None,
            "no task installs reminders",
            id="hand-builds-the-capability",
        ),
        pytest.param(
            reminders_brief,
            _with(
                _INSTALL_REMINDERS,
                "Install reminders with `kit add reminders --wheel "
                "dist/codegen_kit_reminders-0.4.0-py3-none-any.whl`.",
            ),
            "must not install from a wheel file or a built artifact",
            "the plan created no task",
            id="installs-a-supplied-wheel",
        ),
        pytest.param(
            reminders_brief,
            _with(_INSTALL_REMINDERS, "Install it with `kit add reminders-pro`."),
            "which the kit package catalog does not list (installable packages: reminders)",
            "the plan created no task",
            id="invents-a-package",
        ),
        pytest.param(
            shopping_list_brief,
            _with(_SHOPPING_LIST, "Install `kit add reminders` and build the list on it."),
            "catalog_install_requires_plan_install",
            "the plan created no task",
            id="installs-a-package-nothing-asked-for",
        ),
    ],
)
async def test_the_checks_fail_on_a_plan_that_breaks_a_rule(brief, task, refused, failure):
    api, _, seen = await _run(brief(), [task])

    errors = _tool_errors(seen)
    if refused is None:
        assert errors == []
    else:
        assert len(errors) == 1 and refused in errors[0], errors
    check = (
        assert_plan_installs_reminders_from_the_catalog
        if brief is reminders_brief
        else assert_plan_installs_no_package
    )
    if failure == "the plan created no task":
        assert api.task_payloads == []
    else:
        with pytest.raises(AssertionError, match=failure):
            check(api)
