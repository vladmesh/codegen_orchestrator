"""From a capability request to a persisted install task, through the production path.

Run by `tests/runner/activated_snapshot_proof.py` in the runner proof job, in the
orchestrator's environment (`PYTHONPATH=services/langgraph:.`), against the orchestrator
API built from this checkout and its database (`tests/runner/compose.orchestrator.yml`)
and the real activated catalog snapshot. Nothing here is a fake of the orchestrator:

1. the PO's `create_project` tool opens a new order: a draft backend,tg_bot project and
   its pending repository, exactly as a first conversation does; nothing is scaffolded;
2. the PO's `preview_capabilities` tool runs the Architect's resolver over the activated
   snapshot the production reader verifies, and the platform stores the preview;
3. the PO's `present_product_brief` and `confirm_product_brief` tools open and freeze a
   brief with the user's explicit answers — the language and a nonempty list of initial
   channels — and the API derives and stores its plan;
4. the Architect consumer's planning body plans the confirmed brief's first attempt from
   that stored plan on the draft: INSTALL tasks through the API, coverage and the one
   admission, with no model and no in-process wait for the scaffold;
5. install admission refuses the first INSTALL while the project is a draft
   (`workspace_not_ready`): the durable wait the scaffold ends;
6. the INSTALL tasks are read back from the API, and their typed payloads are what the
   kit's runner installs, so the closure installed is the one the database persisted.

The output JSON names every identity on that path for the runner's evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re
from types import SimpleNamespace
import uuid

import structlog

from shared.catalog_activation import CATALOG_ACTIVATION
from shared.clients.internal_api import InternalAPIClient
from shared.contracts.queues.architect import ArchitectMessage
from src.agents.po.tools_briefs import confirm_product_brief, present_product_brief
from src.agents.po.tools_capabilities import MODULE_ROLLOUT_CONFIG_KEY, preview_capabilities
from src.agents.po.tools_projects import create_project
from src.agents.po.tools_shared import init_po_clients
from src.capability_preview import capability_id
from src.catalog_product_settings import PO_CATALOG_CONFIG_KEY
from src.clients.api import api_client
from src.config.settings import get_settings
from src.consumers.architect import _plan
from src.kit_catalog import KitCatalog, get_kit_catalog_reader
from src.llm import channel_usage

#: The explicit answers a user gives to each kind of required question.
ANSWERS = {"product_language": "en", "product_timezone": "UTC"}
#: The capability whose initial channels the user lists.
CHANNELS_PACKAGE = "tg-channels"


class ProductionPathError(RuntimeError):
    pass


def _check(condition: bool, what: str) -> None:
    if not condition:
        raise ProductionPathError(what)


async def _setup(api: InternalAPIClient) -> tuple[int, str, dict]:
    """A new owner's first order: the PO creates a draft project and its pending repository."""
    telegram_id = 424242000 + uuid.uuid4().int % 1000
    user = await api.post_raw(
        "users/", json={"telegram_id": telegram_id, "username": f"runner{telegram_id}"}
    )
    _check(user.status_code == 201, f"user: {user.status_code} {user.text}")
    created = await create_project.ainvoke(
        {
            "title": "Runner proof",
            "modules": "backend,tg_bot",
            "description": "A bot with the ready capabilities the runner proves.",
        },
        config={"configurable": {"telegram_chat_id": str(telegram_id)}},
    )
    found = re.search(r"ID: ([0-9a-f-]{36})", created)
    _check(found is not None, f"no project created: {created}")
    project_id = found.group(1)
    project = (await api.get_raw(f"projects/{project_id}")).json()
    _check(
        project["status"] == "draft" and not project["config"].get("workspace_ready"),
        f"the new order is not a draft: {project}",
    )
    repositories = (await api.get_raw("repositories/", params={"project_id": project_id})).json()
    _check(len(repositories) == 1, f"repositories: {repositories}")
    repository = repositories[0]
    _check(repository["git_url"].startswith("pending://"), f"repository: {repository}")
    # The operator's review step: this project is enabled for module-backed routes.
    rollout = await api.post_raw(
        "system-configs/",
        json={
            "key": MODULE_ROLLOUT_CONFIG_KEY,
            "value": {"project_ids": [project_id]},
            "category": "capabilities",
        },
    )
    _check(rollout.status_code in {200, 201}, f"rollout: {rollout.text}")
    return telegram_id, project_id, repository


def _answer(question: dict, channels: list[str], request_package: dict[str, str]) -> object:
    """The user's explicit answer: the fixed required values, and the initial channels."""
    if question["kind"] == "text_list" and any(
        request_package[request] == CHANNELS_PACKAGE for request in question["request_ids"]
    ):
        return channels
    if question["required"]:
        return ANSWERS[question["question_id"]]
    return None


async def run(packages: list[str], channels: list[str]) -> dict:  # noqa: C901, PLR0915 - one ordered production path
    log = structlog.get_logger("runner_production_plan")
    api = InternalAPIClient(get_settings().api_base_url)
    init_po_clients(api, None)
    telegram_id, project_id, repository = await _setup(api)
    catalog = await get_kit_catalog_reader().read()
    _check(isinstance(catalog, KitCatalog), f"activated catalog unavailable: {catalog}")
    config = {
        "configurable": {
            "thread_id": f"runner-{project_id}",
            "telegram_chat_id": str(telegram_id),
            PO_CATALOG_CONFIG_KEY: catalog,
        }
    }
    offered = {item.name: item for item in catalog.packages}
    # The first request names no id, as the PO may send it: its words are the package's own
    # catalog phrase, so the preview must still route it as that module.
    requests = [
        {
            "request_id": f"r{index}",
            "capability_id": None if index == 1 else capability_id(name),
            "wording": offered[name].package.capabilities[0],
        }
        for index, name in enumerate(packages, start=1)
    ]
    preview = json.loads(
        await preview_capabilities.ainvoke(
            {"project_id": project_id, "requests": requests}, config=config
        )
    )
    _check(preview.get("status") == "previewed", f"preview refused: {preview}")
    _check(
        [route["route"] for route in preview["routes"]] == ["module"] * len(packages),
        f"not every capability is a module route: {preview['routes']}",
    )
    _check(
        [route["capability_id"] for route in preview["routes"]]
        == [capability_id(name) for name in packages],
        f"a route names another capability: {preview['routes']}",
    )
    answers = []
    request_package = {
        request["request_id"]: name for request, name in zip(requests, packages, strict=True)
    }
    for question in preview["questions"]:
        value = _answer(question, channels, request_package)
        if value is None:
            continue
        _check(not question["choices"] or value in question["choices"], f"{question} {value}")
        answers.append(
            {
                "question_id": question["question_id"],
                "kind": question["kind"],
                "value": value,
                "description": f"Chosen: {value}",
            }
        )
    requirements = [
        {
            "id": f"req{index}",
            "text": offered[name].package.summary[:200],
            "user_wording": offered[name].package.capabilities[0][:250],
        }
        for index, name in enumerate(packages, start=1)
    ]
    brief_capabilities = {
        "preview_id": preview["preview_id"],
        "capabilities": [
            {
                "request_id": route["request_id"],
                "capability_id": route["capability_id"],
                "route": "module",
                "requirement_ids": [requirement["id"]],
            }
            for route, requirement in zip(preview["routes"], requirements, strict=True)
        ],
        "answers": answers,
    }
    presented = await present_product_brief.ainvoke(
        {
            "project_id": project_id,
            "title": "Runner proof bot",
            "summary": "A bot with the ready capabilities the runner proves.",
            "must_requirements": requirements,
            "language": "en",
            "usage_examples": [
                {
                    "requirement_id": requirement["id"],
                    "user_sends": "the bot's command",
                    "product_answers": "the module's answer",
                }
                for requirement in requirements
            ],
            "capabilities": brief_capabilities,
        },
        config=config,
    )
    found = re.search(r"\(id: (brief-[0-9a-f]+)\)", presented)
    _check(found is not None, f"no brief presented: {presented}")
    brief_id = found.group(1)
    confirmed = await confirm_product_brief.ainvoke(
        {"project_id": project_id, "brief_id": brief_id}, config=config
    )
    _check("confirmed and frozen" in confirmed, f"not confirmed: {confirmed}")
    plan = await api_client.get_capability_plan(brief_id)
    _check(plan is not None, "the confirmed brief has no stored plan")
    story = await api.post_raw("stories/", json={"project_id": project_id, "title": "Runner"})
    _check(story.status_code == 201, f"story: {story.text}")
    story_id = story.json()["id"]
    bound = await api.post_raw(f"product-briefs/{brief_id}/story", json={"story_id": story_id})
    _check(bound.status_code == 200, f"bind: {bound.text}")
    await api_client.transition_story(story_id, "start")
    message = ArchitectMessage(
        story_id=story_id, project_id=project_id, telegram_chat_id=str(telegram_id)
    )
    with channel_usage() as usage:
        planned = await _plan(
            message,
            SimpleNamespace(redis=None),
            await api_client.get_story(story_id),
            [],
            get_settings(),
            usage,
            log.bind(story_id=story_id),
            scaffold_deferred=True,
        )
    _check(planned.get("status") == "success", f"architect planning: {planned}")
    _check(usage.channels() == [], f"a model was asked: {usage.channels()}")
    tasks = await api_client.get_tasks_by_story(story_id)
    installs = {task.install.package.name: task for task in tasks if task.install is not None}
    _check(sorted(installs) == sorted(packages), f"install tasks {sorted(installs)}")
    _check(len(tasks) == len(installs), f"a task other than the installs: {tasks}")
    stored = {item.install.package.name: item.install for item in plan.modules}
    for name, task in installs.items():
        _check(task.install == stored[name], f"{name}: the task is not the stored closure")
        _check(task.dispatch_admitted, f"{name}: the install task was not released")
        _check(task.repository_id == repository["id"], f"{name}: another repository")
    project = (await api.get_raw(f"projects/{project_id}")).json()
    _check(project["status"] == "draft", f"planning changed the draft: {project['status']}")
    # The INSTALL waits at admission for the scaffold, durably: nothing is queued.
    first = next(task for task in tasks if task.install is not None and not task.blocked_by_task_id)
    waiting = (
        await api.post_raw(f"tasks/{first.id}/catalog-install", json={"action": "admit"})
    ).json()
    _check(
        waiting["outcome"] == "refused" and waiting["reason"] == "workspace_not_ready",
        f"admission before the scaffold: {waiting}",
    )
    await api.close()
    await api_client.close()
    return {
        "project_id": project_id,
        "project_status_at_planning": project["status"],
        "repository": {"id": repository["id"], "git_url": repository["git_url"]},
        "admission_before_scaffold": {
            "task_id": first.id,
            "outcome": waiting["outcome"],
            "reason": waiting["reason"],
        },
        "initial_channels": channels,
        "preview": {key: preview[key] for key in ("preview_id", "routes", "questions")},
        "brief_id": brief_id,
        "story_id": story_id,
        "plan": plan.model_dump(mode="json"),
        "activation": CATALOG_ACTIVATION.model_dump(mode="json"),
        "catalog": {
            "commit": catalog.commit,
            "catalog_sha256": catalog.catalog_sha256,
            "catalog_digest": catalog.digest,
            "source": catalog.source,
        },
        "install_tasks": {
            name: {"task_id": task.id, "install": task.install.model_dump(mode="json")}
            for name, task in installs.items()
        },
        "model_channels": usage.channels(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packages", required=True, help="catalog names, install order")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--initial-channels", required=True, help="the channels the user answers, comma-separated"
    )
    args = parser.parse_args()
    result = asyncio.run(run(args.packages.split(","), args.initial_channels.split(",")))
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
