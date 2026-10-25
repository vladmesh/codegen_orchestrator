"""The dispatcher admits and queues an INSTALL of either target route, never engineering work."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

from _run_routing_factories import _make_task
import pytest

from shared.contracts.dto.catalog_install import CatalogInstall
from shared.contracts.dto.install_operation import InstallDecision, InstallOperation
from shared.contracts.dto.local_install import LocalPackageInstall
from shared.contracts.queues.scaffold import ScaffoldMessage
from shared.queues import SCAFFOLD_QUEUE
from shared.tests.unit.test_local_install import PROJECT, catalog_install, local_install

CATALOG = CatalogInstall.model_validate(catalog_install())
LOCAL = LocalPackageInstall.model_validate(local_install())
CYCLE = datetime(2026, 1, 1, tzinfo=UTC)


def _install_task(task_id, payload):
    return _make_task(
        id=task_id,
        project_id=PROJECT,
        type="install",
        story_id="story-1",
        repository_id="repo-1",
        install=payload,
    )


def _admitted(task_id, install):
    return InstallDecision(
        outcome="admitted",
        operation=InstallOperation(
            id=f"install-{task_id}",
            project_id=PROJECT,
            task_id=task_id,
            story_id="story-1",
            repository_id="repo-1",
            cycle_started_at=CYCLE,
            state="queued",
            stage="queued",
        ),
        install=install,
        git_url="https://github.com/synthetic/notes",
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


def _published(redis):
    return [call.args for call in redis.publish_message.await_args_list]


@pytest.mark.asyncio
async def test_a_local_target_is_queued_as_its_admitted_operation_and_target(clients):
    from src.tasks.task_dispatcher import dispatch_todo_tasks

    api, redis = clients
    api.get_tasks_by_status.return_value = [_install_task("local-1", local_install())]
    api.catalog_install_command.return_value = _admitted("local-1", LOCAL)

    assert await dispatch_todo_tasks(api, redis) == 1
    [(task_id, command)] = [call.args for call in api.catalog_install_command.await_args_list]
    assert (task_id, command.action) == ("local-1", "admit")
    [(queue, message)] = _published(redis)
    assert queue == SCAFFOLD_QUEUE
    decoded = ScaffoldMessage.model_validate_json(message.model_dump_json())
    assert decoded.install == LOCAL and isinstance(decoded.install, LocalPackageInstall)
    assert (
        decoded.mode,
        decoded.project_id,
        decoded.repository_id,
        decoded.task_id,
        decoded.story_id,
        decoded.operation_id,
        decoded.cycle_started_at,
        decoded.project_name,
    ) == (
        "install",
        str(PROJECT),
        "repo-1",
        "local-1",
        "story-1",
        "install-local-1",
        CYCLE,
        "notes",
    )
    redis.redis.set.assert_awaited_once_with(
        "catalog-install:queued:install-local-1", "1", nx=True, ex=60
    )
    api.admit_engineering_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_a_refused_local_admission_publishes_nothing(clients):
    from src.tasks.task_dispatcher import dispatch_todo_tasks

    api, redis = clients
    api.get_tasks_by_status.return_value = [_install_task("local-1", local_install())]
    api.catalog_install_command.return_value = InstallDecision(
        outcome="refused", reason="blocked_by_open_task"
    )

    assert await dispatch_todo_tasks(api, redis) == 0
    redis.redis.set.assert_not_called()
    redis.publish_message.assert_not_called()
    api.admit_engineering_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_a_throttled_operation_is_not_published_twice(clients):
    from src.tasks.task_dispatcher import dispatch_todo_tasks

    api, redis = clients
    api.get_tasks_by_status.return_value = [_install_task("local-1", local_install())]
    api.catalog_install_command.return_value = _admitted("local-1", LOCAL)
    redis.redis.set.return_value = None

    assert await dispatch_todo_tasks(api, redis) == 0
    redis.publish_message.assert_not_called()


@pytest.mark.asyncio
async def test_a_catalog_target_beside_a_local_one_is_still_admitted_and_queued(clients):
    from src.tasks.task_dispatcher import dispatch_todo_tasks

    api, redis = clients
    api.get_tasks_by_status.return_value = [
        _install_task("local-1", local_install()),
        _install_task("catalog-1", catalog_install()),
    ]
    api.catalog_install_command.side_effect = [
        _admitted("local-1", LOCAL),
        _admitted("catalog-1", CATALOG),
    ]

    assert await dispatch_todo_tasks(api, redis) == 2
    messages = [message for queue, message in _published(redis) if queue == SCAFFOLD_QUEUE]
    assert [(message.operation_id, message.install) for message in messages] == [
        ("install-local-1", LOCAL),
        ("install-catalog-1", CATALOG),
    ]
    api.admit_engineering_dispatch.assert_not_called()
