"""Two briefs the architect resolves against the kit's package catalog, scripted and real.

Sprint 1477, DoD4: the architect resolves a capability against the catalog by itself.
A confirmed brief asking for one-time reminders yields a plan that installs the
`reminders` package the catalog lists, as `kit add reminders` with no supplied artifact,
and whose criteria name what the install leaves true: the package in the backend
manifest, the regenerated contract recording it, and the capability observable from
outside. A brief whose capability no catalog package covers — a shopping list, which the
product builds in its own backend — yields a plan with no `kit add` at all.

Both runs apply these checks to what the real tools sent to the stub API: the scripted
run in CI (`tests/unit/test_architect_kit_catalog_plan.py`) and the real-LLM run, skipped
without a key, against the live catalog (`tests/e2e/test_architect_kit_catalog_plan.py`).
The checks read the plan, not the `create_task` refusal: a refused task never reaches
the API, so a plan the refusal stopped fails them for its missing install.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from unittest.mock import MagicMock, patch

from shared.contracts.dto.product_brief import ProductBriefContent
from shared.contracts.queues.architect import ArchitectMessage
from tests.unit.factories import make_product_brief, make_repository, make_story
from tests.unit.test_architect_consumer import _FakeBriefBoundary, _FakeRedis

STORY_ID = "story-kit-catalog"
TELEGRAM_CHAT_ID = "4242"
PREVIOUS_CRITERIA = "- GET /health returns 200"

REMIND = "remind"
SHOPPING_ADD = "shopping-add"
SHOPPING_LIST = "shopping-list"

#: The one package the reminders brief is covered by, by its catalog name.
REMINDERS = "reminders"


@dataclass(frozen=True)
class CatalogBrief:
    """A confirmed brief and the story it backs."""

    title: str
    description: str
    content: ProductBriefContent


def reminders_brief() -> CatalogBrief:
    """One-time text reminders: what the catalog's `reminders` package provides."""
    wording = "I want the bot to remind me about things at a time I name, once."
    return CatalogBrief(
        title="Reminder bot",
        description="A Telegram bot that sends me one-time text reminders at a time I name.",
        content=ProductBriefContent(
            summary="The bot reminds me once, at the time I name, with the text I gave it.",
            language="en",
            must_requirements=[
                {
                    "id": REMIND,
                    "text": "Schedules a one-time text reminder and sends it when it is due",
                    "user_wording": wording,
                }
            ],
            usage_examples=[
                {
                    "requirement_id": REMIND,
                    "user_sends": "/remind 2026-10-05 09:00 call the bank",
                    "product_answers": "Reminder set for 2026-10-05 09:00: call the bank",
                }
            ],
            limitations=["A reminder fires once; there are no repeating reminders."],
        ),
    )


def shopping_list_brief() -> CatalogBrief:
    """A shopping list: a capability no catalog package covers."""
    wording = "I want to keep my shopping list in the bot: add things and see the list."
    return CatalogBrief(
        title="Shopping list bot",
        description="A Telegram bot that keeps my shopping list.",
        content=ProductBriefContent(
            summary="The bot keeps my shopping list: I add items and ask for the list.",
            language="en",
            must_requirements=[
                {"id": SHOPPING_ADD, "text": "Adds an item to my list", "user_wording": wording},
                {"id": SHOPPING_LIST, "text": "Shows my list", "user_wording": wording},
            ],
            usage_examples=[
                {
                    "requirement_id": SHOPPING_ADD,
                    "user_sends": "/add milk",
                    "product_answers": "Added: milk",
                },
                {
                    "requirement_id": SHOPPING_LIST,
                    "user_sends": "/list",
                    "product_answers": "Your list: milk",
                },
            ],
        ),
    )


class CatalogPlanApi(_FakeBriefBoundary):
    """The released brief boundary in memory, plus what the plan wrote outside it."""

    def __init__(self, brief: CatalogBrief):
        super().__init__(make_product_brief(story_id=STORY_ID, content=brief.content))
        self.story = brief
        self.content = brief.content
        self.task_payloads: list[dict] = []
        self.criteria: str | None = None
        #: `po:input` as the consumer publishes to it.
        self.redis = _FakeRedis()

    async def get_story(self, story_id):
        return make_story(
            id=story_id,
            status="created",
            title=self.story.title,
            description=self.story.description,
        )

    async def create_task(self, task_data):
        self.task_payloads.append(task_data)
        return await super().create_task(task_data)

    async def get_primary_repository(self, project_id):
        return make_repository(acceptance_criteria=self.criteria or PREVIOUS_CRITERIA)

    async def update_repository(self, repo_id, data):
        self.criteria = data["acceptance_criteria"]
        return make_repository(id=repo_id, acceptance_criteria=self.criteria)


async def plan_story(api: CatalogPlanApi, *, model: str, base_url: str, api_key: str) -> dict:
    """Run the architect consumer on the brief, with `api` behind the consumer and every tool."""
    settings = MagicMock(
        architect_llm_model=model,
        architect_llm_base_url=base_url,
        architect_llm_api_key=api_key,
    )
    job = ArchitectMessage(
        story_id=STORY_ID,
        project_id=str(api.brief.project_id),
        telegram_chat_id=TELEGRAM_CHAT_ID,
    ).model_dump(mode="json")
    with (
        patch("src.consumers.architect.api_client", api),
        patch("src.agents.architect.tools.api_client", api),
        patch("src.consumers.architect.get_settings", return_value=settings),
    ):
        from src.consumers.architect import process_architect_job

        return await process_architect_job(job, api.redis)


#: Every `kit add` in a task and the name it installs, however the command is quoted.
_KIT_ADD = re.compile(r"(?<![\w-])kit\s+add\b[\s`'\"]*(?P<name>[^\s`'\"]*)")
_SUPPLIED_ARTIFACT = re.compile(r"--wheel\b|\.whl\b", re.IGNORECASE)
_MANIFEST = re.compile(r"manifest", re.IGNORECASE)
_REGENERATED_CONTRACT = re.compile(
    r"(?:re)?generat\w*[^\n]*contract|contract[^\n]*(?:re)?generat|_active_packages",
    re.IGNORECASE,
)
_OBSERVABLE = re.compile(r"\bGET /|\bPOST /|telegram|FIRE JOB", re.IGNORECASE)


def _text(task: dict) -> str:
    return f"{task['description']}\n{task['acceptance_criteria']}"


def installed_packages(api: CatalogPlanApi) -> dict[str, list[str]]:
    """`{package name: [title of each task that runs kit add <name>]}`."""
    installs: dict[str, list[str]] = {}
    for task in api.task_payloads:
        for match in _KIT_ADD.finditer(_text(task)):
            installs.setdefault(match.group("name").rstrip(".,;:)"), []).append(task["title"])
    return installs


def assert_no_task_installs_a_supplied_artifact(api: CatalogPlanApi) -> None:
    for task in api.task_payloads:
        assert not _SUPPLIED_ARTIFACT.search(_text(task)), (
            f"task {task['title']!r} installs a supplied artifact: {_text(task)}"
        )


def assert_plan_installs_reminders_from_the_catalog(api: CatalogPlanApi) -> None:
    """A task installs `reminders` as `kit add reminders`, and its criteria say what holds."""
    assert api.task_payloads, "the plan created no task"
    installs = installed_packages(api)
    assert REMINDERS in installs, (
        f"no task installs {REMINDERS} with `kit add {REMINDERS}`: "
        f"{[_text(task) for task in api.task_payloads]}"
    )
    assert set(installs) == {REMINDERS}, f"the plan installs more than {REMINDERS}: {installs}"
    assert_no_task_installs_a_supplied_artifact(api)
    for task in api.task_payloads:
        if task["title"] not in installs[REMINDERS]:
            continue
        criteria = task["acceptance_criteria"]
        assert _MANIFEST.search(criteria), f"criteria do not name the manifest: {criteria}"
        assert _REGENERATED_CONTRACT.search(criteria), (
            f"criteria do not name the regenerated contract: {criteria}"
        )
        assert _OBSERVABLE.search(criteria), f"criteria name no observable: {criteria}"


def assert_plan_installs_no_package(api: CatalogPlanApi) -> None:
    """No task runs `kit add`: no catalog package covers what the brief asks for."""
    for task in api.task_payloads:
        assert not _KIT_ADD.search(_text(task)), (
            f"task {task['title']!r} installs a package: {_text(task)}"
        )
    assert_no_task_installs_a_supplied_artifact(api)


def assert_every_requirement_is_disposed(api: CatalogPlanApi) -> None:
    must = {requirement.id for requirement in api.content.must_requirements}
    assert set(api.coverage) == must, f"undisposed requirements: {must - set(api.coverage)}"
