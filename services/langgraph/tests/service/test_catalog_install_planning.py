"""No-model planning through the real API, coverage, scheduler and Redis boundaries."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

from psycopg import AsyncConnection
import pytest

from shared.catalog_activation import CATALOG_ACTIVATION
from shared.contracts.dto.local_install import LocalPackageBuild
from shared.contracts.dto.story_planning import PlanningChannels
from shared.contracts.queues.scaffold import ScaffoldMessage
from shared.queues import ENGINEERING_QUEUE, SCAFFOLD_QUEUE, WORKER_COMMANDS
from src.agents.architect import tools
from src.catalog_install import INSTALL_PYTHON_VERSION
from src.clients.api import LanggraphAPIClient
from src.kit_catalog import install_snapshot
from src.planning_module_jobs import PlanningModuleFacts, plan_module_install
from src.scripted_install_plan import scripted_install_plan
from tests.unit.activated_catalog import activated_catalog
from tests.unit.factories import make_task
from tests.unit.test_catalog_install import snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("transfer_claim", [False, True])
async def test_scripted_selection_persists_one_install_and_dispatches_without_engineering(
    real_redis,
    transfer_claim,
):
    api = LanggraphAPIClient()
    api.base_url = os.environ["TEST_API_BASE_URL"]
    telegram = uuid.uuid4().int % 1_000_000_000
    before = {queue: await real_redis.xlen(queue) for queue in (ENGINEERING_QUEUE, WORKER_COMMANDS)}
    try:
        await api.post("users/", json={"telegram_id": telegram, "username": "catalog-owner"})
        project = await api.post(
            "projects/",
            headers={"X-Telegram-ID": str(telegram)},
            json={
                "title": "Owned notes",
                "status": "active",
                "initiating_run_id": "catalog-init",
                "config": {"workspace_ready": True, "modules": ["backend", "tg_bot"]},
            },
        )
        pid = project["id"]
        repo = await api.post(
            "repositories/",
            json={
                "project_id": pid,
                "role": "primary",
                "name": f"notes-{telegram}",
                "git_url": f"https://github.com/synthetic/notes-{telegram}",
            },
        )
        story = await api.post(
            "stories/", json={"project_id": pid, "title": "Add catalog reminders"}
        )
        content = {
            "summary": "Add reminders to my notes bot",
            "language": "en",
            "must_requirements": [
                {
                    "id": "remind",
                    "text": "Confirm, send and cancel a one-time reminder",
                    "user_wording": "Add the catalog reminders capability",
                }
            ],
            "usage_examples": [
                {
                    "requirement_id": "remind",
                    "user_sends": "Remind me to call Sam tomorrow at 10",
                    "product_answers": "Confirm the reminder, then list or cancel it",
                }
            ],
        }
        brief = await api.post(
            "product-briefs/",
            json={
                "project_id": pid,
                "title": "Add reminders",
                "content": content,
                "request_id": f"request-{telegram}",
            },
        )
        await api.post(
            f"product-briefs/{brief['id']}/confirm",
            json={"request_id": f"confirm-{telegram}", "content": content},
        )
        await api.post(f"product-briefs/{brief['id']}/story", json={"story_id": story["id"]})
        claim = await api.claim_planning_attempt(brief["id"]) if transfer_claim else None
        if claim is not None:
            assert claim.outcome == "claimed"
        reader = AsyncMock()
        reader.read.return_value = snapshot()
        # Only catalog transport is controlled. This resource corpus is copied
        # byte-for-byte from the released kit; closure and all persistence are real.
        with (
            patch("src.scripted_install_plan.api_client", api),
            patch("src.agents.architect.tools.api_client", api),
            patch("src.scripted_install_plan.get_kit_catalog_reader", return_value=reader),
            patch(
                "src.llm.openrouter.ChatOpenAI", side_effect=AssertionError("model is forbidden")
            ),
        ):
            planned = await scripted_install_plan(
                pid,
                story["id"],
                "reminders",
                ["remind"],
                planning_attempt_id=claim.planning_attempt_id if claim is not None else None,
            )
        assert "error" not in planned, planned
        assert planned["coverage_outcome"] == "admitted", planned
        tasks = await api.get_tasks_by_story(story["id"])
        assert len(tasks) == 1
        task = tasks[0]
        assert task.type == "install" and task.repository_id == repo["id"]
        if claim is not None:
            assert task.planning_attempt_id == claim.planning_attempt_id
        assert task.install.package.version == "0.5.0" and task.dispatch_admitted is True
        coverage = await api.list_requirement_coverage(brief["id"])
        assert len(coverage) == 1 and coverage[0].task_id == task.id
        assert coverage[0].planning_attempt_id == task.planning_attempt_id
        dispatched = subprocess.run(
            [sys.executable, "-P", str(Path(__file__).with_name("_catalog_install_scheduler.py"))],
            env=os.environ
            | {
                "PYTHONPATH": "/app/scheduler:/app",
                "INSTALL_TASK": task.id,
                "API_BASE_URL": os.environ["TEST_API_BASE_URL"],
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert dispatched.returncode == 0, dispatched.stdout + dispatched.stderr
        messages = await real_redis.xrange(SCAFFOLD_QUEUE)
        installed = [
            ScaffoldMessage.model_validate_json(fields[b"data"])
            for _, fields in messages
            if json.loads(fields[b"data"]).get("task_id") == task.id
        ]
        assert len(installed) == 1
        assert installed[0].install == task.install and installed[0].mode == "install"
        assert installed[0].operation_id
        assert before == {queue: await real_redis.xlen(queue) for queue in before}
        async with await AsyncConnection.connect(
            os.environ["TEST_DATABASE_URL"], autocommit=True
        ) as db:
            for table in ("runs", "engineering_attempt_ledger", "engineering_budget_reservations"):
                async with db.cursor() as cursor:
                    await cursor.execute(
                        f"SELECT count(*) FROM {table} WHERE project_id=%s", (pid,)
                    )
                    assert (await cursor.fetchone())[0] == 0
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_architect_source_build_persists_owned_intent_chain_and_admitted_coverage(
    real_redis,
):
    """Real tools and API: INSTALL, then the owned source FEATURE, then ordinary work.

    No model, worker, GitHub, package source or native install takes part.
    """
    api = LanggraphAPIClient()
    api.base_url = os.environ["TEST_API_BASE_URL"]
    telegram = uuid.uuid4().int % 1_000_000_000
    before = {queue: await real_redis.xlen(queue) for queue in (ENGINEERING_QUEUE, WORKER_COMMANDS)}
    build = {
        "path": "packages/post-ranking",
        "package": {
            "name": "post-ranking",
            "distribution": "codegen-kit-post-ranking",
            "version": "0.1.0",
        },
        "core_version": CATALOG_ACTIVATION.core_version,
        "python_version": INSTALL_PYTHON_VERSION,
        "tooling_commit": CATALOG_ACTIVATION.tooling_commit,
    }
    try:
        await api.post("users/", json={"telegram_id": telegram, "username": "ranking-owner"})
        project = await api.post(
            "projects/",
            headers={"X-Telegram-ID": str(telegram)},
            json={
                "title": "Ranked digest",
                "status": "active",
                "initiating_run_id": "ranking-init",
                "config": {"workspace_ready": True, "modules": ["backend", "tg_bot"]},
            },
        )
        pid = project["id"]
        repo = await api.post(
            "repositories/",
            json={
                "project_id": pid,
                "role": "primary",
                "name": f"ranked-{telegram}",
                "git_url": f"https://github.com/synthetic/ranked-{telegram}",
            },
        )
        story = await api.post("stories/", json={"project_id": pid, "title": "Rank my posts"})
        content = {
            "summary": "Rank collected posts and remind me to read them",
            "language": "en",
            "must_requirements": [
                {
                    "id": "rank",
                    "text": "Rank collected posts by my configured signals",
                    "user_wording": "Show my posts ranked",
                }
            ],
            "usage_examples": [
                {
                    "requirement_id": "rank",
                    "user_sends": "/top",
                    "product_answers": "The collected posts, highest ranked first",
                }
            ],
        }
        brief = await api.post(
            "product-briefs/",
            json={
                "project_id": pid,
                "title": "Ranked posts",
                "content": content,
                "request_id": f"ranking-request-{telegram}",
            },
        )
        await api.post(
            f"product-briefs/{brief['id']}/confirm",
            json={"request_id": f"ranking-confirm-{telegram}", "content": content},
        )
        await api.post(f"product-briefs/{brief['id']}/story", json={"story_id": story["id"]})
        claim = await api.claim_planning_attempt(brief["id"])
        assert claim.outcome == "claimed"
        attempt = claim.planning_attempt_id
        await api.transition_story(story["id"], "start")
        identity = {"story_id": story["id"], "project_id": pid, "planning_attempt_id": attempt}
        tools.reset_task_chain()
        with patch("src.agents.architect.tools.api_client", api):
            install = await tools.plan_install.coroutine(
                name="reminders",
                kit_install_snapshot=install_snapshot(snapshot()),
                config={"configurable": {}},
                **identity,
            )
            ordinary = {"type": "feature", "kit_catalog_packages": sorted(snapshot().names)}
            source = await tools.create_task.ainvoke(
                {
                    **identity,
                    **ordinary,
                    "title": "Post ranking package",
                    "description": "Rank collected posts by configured signals.",
                    "acceptance_criteria": "The package ranks posts by its settings.",
                    "package_build": build,
                }
            )
            follow_up = await tools.create_task.ainvoke(
                {
                    **identity,
                    **ordinary,
                    "title": "Show ranked posts",
                    "description": "Reply to /top with the ranked posts.",
                    "acceptance_criteria": "Telegram: /top lists posts highest ranked first",
                }
            )
            coverage = await tools.record_requirement_coverage.ainvoke(
                {
                    "requirement_id": "rank",
                    "task_id": source["id"],
                    "brief_id": brief["id"],
                    "planning_attempt_id": attempt,
                }
            )
        assert "error" not in install and "error" not in source, (install, source)
        assert "error" not in follow_up and "error" not in coverage, (follow_up, coverage)
        admitted = await api.admit_product_brief_coverage(
            brief["id"], attempt, channels=PlanningChannels()
        )
        assert admitted.outcome == "admitted"

        tasks = {task.id: task for task in await api.get_tasks_by_story(story["id"])}
        assert set(tasks) == {install["id"], source["id"], follow_up["id"]}
        installed, built, after = (tasks[t["id"]] for t in (install, source, follow_up))
        assert installed.type == "install" and installed.repository_id == repo["id"]
        assert built.type == "feature" and built.status == "todo" and built.install is None
        assert built.package_build == LocalPackageBuild.model_validate(build)
        assert (built.repository_id, built.story_id) == (repo["id"], story["id"])
        assert built.planning_attempt_id == attempt and built.blocked_by_task_id == installed.id
        assert after.package_build is None and after.blocked_by_task_id == built.id
        assert all(task.dispatch_admitted for task in tasks.values())
        [recorded] = await api.list_requirement_coverage(brief["id"])
        assert recorded.task_id == built.id and recorded.planning_attempt_id == attempt

        dispatched = subprocess.run(
            [sys.executable, "-P", str(Path(__file__).with_name("_catalog_install_scheduler.py"))],
            env=os.environ
            | {
                "PYTHONPATH": "/app/scheduler:/app",
                "INSTALL_TASK": installed.id,
                "API_BASE_URL": os.environ["TEST_API_BASE_URL"],
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert dispatched.returncode == 0, dispatched.stdout + dispatched.stderr
        assert before == {queue: await real_redis.xlen(queue) for queue in before}
        async with await AsyncConnection.connect(
            os.environ["TEST_DATABASE_URL"], autocommit=True
        ) as db:
            for table in ("runs", "engineering_attempt_ledger", "engineering_budget_reservations"):
                async with db.cursor() as cursor:
                    await cursor.execute(
                        f"SELECT count(*) FROM {table} WHERE project_id=%s", (pid,)
                    )
                    assert (await cursor.fetchone())[0] == 0
        assert (await api.get_task(built.id)).blocked_by_task_id == installed.id
    finally:
        tools.reset_task_chain()
        await api.close()


@pytest.fixture(scope="session")
def activated_kit_catalog():
    return activated_catalog()


@pytest.fixture
def criteria_api(monkeypatch):
    # Only API transport is doubled. Restoration, binding/manifest/schema checks
    # and selected-closure comparison exercise the native planning boundary.
    api = AsyncMock()
    api.get_primary_repository.return_value = SimpleNamespace(id="repo-1")
    api.update_repository.side_effect = lambda repo, body: SimpleNamespace(id=repo, **body)
    monkeypatch.setattr(tools, "api_client", api)
    return api


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["catalog", "binding", "manifest", "activation", "duplicate"])
async def test_explicit_criteria_rechecks_changed_inputs_before_writing(
    criteria_api, activated_kit_catalog, changed
):
    injected = deepcopy(install_snapshot(activated_kit_catalog))
    facts = PlanningModuleFacts()
    catalog = facts.restore(injected)
    install, _ = plan_module_install(catalog, "reminders", INSTALL_PYTHON_VERSION, facts=facts)
    tasks = [make_task(type="install", install=install)]
    if changed == "catalog":
        injected["catalog"] += "\n# changed\n"
    elif changed == "binding":
        injected["bindings"]["reminders"] = injected["bindings"]["reminders"].replace(
            "textparse.when", "invented.when"
        )
    elif changed == "manifest":
        injected["manifests"]["reminders"] = "jobs_schema: ["
    elif changed == "activation":
        injected["commit"] = "c" * 40
    else:
        tasks.append(
            make_task(
                type="install", install=install.model_copy(update={"catalog_digest": "0" * 64})
            )
        )
    criteria_api.get_tasks_by_story.return_value = tasks

    result = await tools.update_acceptance_criteria.ainvoke(
        {
            "project_id": "project-1",
            "story_id": "story-1",
            "acceptance_criteria": "- GET /health returns 200",
            "selected_module_jobs": {},
            "must_requirements": [],
            "kit_install_snapshot": injected,
        },
        config={"configurable": {"planning_module_facts": facts}},
    )
    assert "Selected module job contract refused" in result["error"]
    criteria_api.update_repository.assert_not_awaited()
