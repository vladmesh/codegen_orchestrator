"""From a capability request to a persisted install task, through the production path.

Run by `tests/runner/activated_snapshot_proof.py` in the runner proof job, in the
orchestrator's environment (`PYTHONPATH=services/langgraph:.`), against the orchestrator
API built from this checkout and its database (`tests/runner/compose.orchestrator.yml`)
and the real activated catalog snapshot. Nothing here is a fake of the orchestrator:

1. the PO's `preview_capabilities` tool runs the Architect's resolver over the activated
   snapshot the production reader verifies, and the platform stores the preview;
2. the PO's `present_product_brief` and `confirm_product_brief` tools open and freeze a
   brief with the user's explicit answers, and the API derives and stores its plan;
3. the Architect consumer's planning body plans the confirmed brief's first attempt from
   that stored plan: INSTALL tasks through the API, coverage and the one admission;
4. the INSTALL tasks are read back from the API, and their typed payloads are what the
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


class ProductionPathError(RuntimeError):
    pass


def _check(condition: bool, what: str) -> None:
    if not condition:
        raise ProductionPathError(what)


async def _setup(api: InternalAPIClient) -> tuple[int, str]:
    telegram_id = 424242000 + uuid.uuid4().int % 1000
    user = await api.post_raw(
        "users/", json={"telegram_id": telegram_id, "username": f"runner{telegram_id}"}
    )
    _check(user.status_code == 201, f"user: {user.status_code} {user.text}")
    project_id = str(uuid.uuid4())
    project = await api.post_raw(
        "projects/",
        headers={"X-Telegram-ID": str(telegram_id)},
        json={
            "id": project_id,
            "title": "Runner proof",
            "initiating_run_id": f"runner-{uuid.uuid4().hex}",
            "status": "active",
            "config": {"workspace_ready": True, "modules": ["backend", "tg_bot"]},
        },
    )
    _check(project.status_code == 201, f"project: {project.status_code} {project.text}")
    repository = await api.post_raw(
        "repositories/",
        json={
            "project_id": project_id,
            "name": "runner-product",
            "git_url": "https://github.com/ci/runner-product",
        },
    )
    _check(repository.status_code == 201, f"repository: {repository.text}")
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
    return telegram_id, project_id


async def run(packages: list[str]) -> dict:  # noqa: PLR0915 - one ordered production path
    log = structlog.get_logger("runner_production_plan")
    api = InternalAPIClient(get_settings().api_base_url)
    init_po_clients(api, None)
    telegram_id, project_id = await _setup(api)
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
    requests = [
        {
            "request_id": f"r{index}",
            "capability_id": capability_id(name),
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
    answers = []
    for question in preview["questions"]:
        if not question["required"]:
            continue
        value = ANSWERS[question["question_id"]]
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
                "request_id": request["request_id"],
                "capability_id": request["capability_id"],
                "route": "module",
                "requirement_ids": [requirement["id"]],
            }
            for request, requirement in zip(requests, requirements, strict=True)
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
        )
    _check(planned.get("status") == "success", f"architect planning: {planned}")
    _check(usage.channels() == [], f"a model was asked: {usage.channels()}")
    tasks = await api_client.get_tasks_by_story(story_id)
    installs = {task.install.package.name: task for task in tasks if task.install is not None}
    _check(sorted(installs) == sorted(packages), f"install tasks {sorted(installs)}")
    stored = {item.install.package.name: item.install for item in plan.modules}
    for name, task in installs.items():
        _check(task.install == stored[name], f"{name}: the task is not the stored closure")
        _check(task.dispatch_admitted, f"{name}: the install task was not released")
    await api.close()
    await api_client.close()
    return {
        "project_id": project_id,
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
    args = parser.parse_args()
    result = asyncio.run(run(args.packages.split(",")))
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
