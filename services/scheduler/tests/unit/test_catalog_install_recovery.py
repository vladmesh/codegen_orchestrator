"""The dispatcher re-admits only the install tasks whose operation is still running.

A real `SchedulerAPIClient` talks over `httpx.MockTransport` to a fake API that
applies the task list filters the way `GET /tasks/` does.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json

from _run_routing_factories import _make_task
import httpx
import pytest

from shared.contracts.dto.catalog_install import InstallOperation

PROJECT_ID = "00000000-0000-0000-0000-000000000001"
_FILTERS = ("status", "type", "install_state")


def _install(task_id: str, status: str, state: str):
    return _make_task(
        id=task_id,
        type="install",
        status=status,
        story_id="story-1",
        repository_id="repo-1",
        install_operation=InstallOperation(
            id=f"op-{task_id}",
            project_id=PROJECT_ID,
            task_id=task_id,
            story_id="story-1",
            repository_id="repo-1",
            cycle_started_at=datetime.now(UTC),
            state=state,
            stage="claimed" if state == "running" else "published",
        ),
    )


class _FakeAPI:
    def __init__(self, tasks, failing: frozenset[str] = frozenset()) -> None:
        self._tasks = tasks
        self._failing = failing
        self.listings: list[dict[str, str]] = []
        self.admitted: list[str] = []

    def _matches(self, task, params: dict[str, str]) -> bool:
        operation = task.install_operation
        row = {
            "status": task.status,
            "type": task.type,
            "install_state": operation.state if operation is not None else None,
        }
        return all(row[key] == value for key, value in params.items())

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/tasks/":
            params = dict(request.url.params)
            assert set(params) <= set(_FILTERS), params
            self.listings.append(params)
            rows = [t.model_dump(mode="json") for t in self._tasks if self._matches(t, params)]
            return httpx.Response(200, json=rows)
        if request.method == "POST" and path.endswith("/catalog-install"):
            task_id = path.split("/")[-2]
            assert json.loads(request.content)["action"] == "admit"
            self.admitted.append(task_id)
            if task_id in self._failing:
                return httpx.Response(503, json={"detail": "API restarting"})
            return httpx.Response(200, json={"outcome": "reused"})
        raise AssertionError(f"unexpected API request: {request.method} {request.url}")


@pytest.fixture
def api_factory(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("API_BASE_URL", "http://api.test")

    def build(fake: _FakeAPI):
        from src.clients.api import SchedulerAPIClient

        client = SchedulerAPIClient()
        client._client = httpx.AsyncClient(
            base_url="http://api.test", transport=httpx.MockTransport(fake.handle)
        )
        return client

    return build


def _roster():
    return [
        _install("install-dev-running", "in_dev", "running"),
        _install("install-cancelled-running", "cancelled", "running"),
        _install("install-cancelled-settled", "cancelled", "recovery_required"),
        _make_task(id="feature-cancelled", type="feature", status="cancelled"),
    ]


@pytest.mark.asyncio
async def test_recovery_lists_only_running_installs_and_admits_them(api_factory):
    from src.tasks.task_dispatcher import _recover_catalog_installs

    fake = _FakeAPI(_roster())

    await _recover_catalog_installs(api_factory(fake))

    assert fake.listings == [
        {"status": "in_dev", "type": "install", "install_state": "running"},
        {"status": "cancelled", "type": "install", "install_state": "running"},
    ]
    assert fake.admitted == ["install-dev-running", "install-cancelled-running"]


@pytest.mark.asyncio
async def test_a_failed_admit_does_not_stop_the_next_one(api_factory):
    from src.tasks.task_dispatcher import _recover_catalog_installs

    fake = _FakeAPI(_roster(), failing=frozenset({"install-dev-running"}))

    await _recover_catalog_installs(api_factory(fake))

    assert fake.admitted == ["install-dev-running", "install-cancelled-running"]
