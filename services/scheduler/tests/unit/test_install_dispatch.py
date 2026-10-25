"""The dispatcher admits and queues a catalog INSTALL, and never a product-local one."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

from _run_routing_factories import _make_task
from pydantic import ValidationError
import pytest

from shared.contracts.dto.catalog_install import CatalogInstall
from shared.contracts.dto.install_operation import InstallDecision, InstallOperation
from shared.contracts.queues.scaffold import ScaffoldMessage
from shared.queues import SCAFFOLD_QUEUE
from shared.tests.unit.test_local_install import PROJECT, catalog_install, local_install

CATALOG = CatalogInstall.model_validate(catalog_install())


def _install_task(task_id, payload):
    return _make_task(
        id=task_id,
        project_id=PROJECT,
        type="install",
        story_id="story-1",
        repository_id="repo-1",
        install=payload,
    )


@pytest.fixture
def clients(monkeypatch):
    monkeypatch.setattr(
        "src.tasks.scaffold_trigger._template_config",
        lambda: ("gh:vladmesh/codegen-product-kit", "v1"),
    )
    api = AsyncMock()
    redis = AsyncMock()
    redis.redis.set.return_value = True
    return api, redis


@pytest.mark.asyncio
async def test_a_local_target_is_neither_admitted_nor_queued(clients):
    from src.tasks.task_dispatcher import dispatch_todo_tasks

    api, redis = clients
    api.get_tasks_by_status.return_value = [_install_task("local-1", local_install())]

    assert await dispatch_todo_tasks(api, redis) == 0
    api.catalog_install_command.assert_not_called()
    redis.publish_message.assert_not_called()


@pytest.mark.asyncio
async def test_a_catalog_target_beside_it_is_still_admitted_and_queued(clients):
    from src.tasks.task_dispatcher import dispatch_todo_tasks

    api, redis = clients
    operation = InstallOperation(
        id="install-1",
        project_id=PROJECT,
        task_id="catalog-1",
        story_id="story-1",
        repository_id="repo-1",
        cycle_started_at=datetime.now(UTC),
        state="queued",
        stage="queued",
    )
    api.get_tasks_by_status.return_value = [
        _install_task("local-1", local_install()),
        _install_task("catalog-1", catalog_install()),
    ]
    api.catalog_install_command.return_value = InstallDecision(
        outcome="admitted",
        operation=operation,
        install=CATALOG,
        git_url="https://github.com/synthetic/notes",
    )

    assert await dispatch_todo_tasks(api, redis) == 1
    [(task_id, command)] = [call.args for call in api.catalog_install_command.await_args_list]
    assert (task_id, command.action) == ("catalog-1", "admit")
    [(queue, message)] = [call.args for call in redis.publish_message.await_args_list]
    assert queue == SCAFFOLD_QUEUE and isinstance(message, ScaffoldMessage)
    assert (message.operation_id, message.install) == ("install-1", CATALOG)


def test_a_scaffold_message_never_carries_a_local_target():
    fields = {
        "project_id": str(PROJECT),
        "repository_id": "repo-1",
        "template_repo": "gh:vladmesh/codegen-product-kit",
        "template_ref": "v1",
        "project_name": "notes",
        "modules": "backend,tg_bot",
        "mode": "install",
        "task_id": "install-1",
        "story_id": "story-1",
        "operation_id": "install-1",
        "cycle_started_at": datetime.now(UTC),
    }
    assert ScaffoldMessage(**fields, install=CATALOG).install == CATALOG
    with pytest.raises(ValidationError):
        ScaffoldMessage(**fields, install=local_install())
