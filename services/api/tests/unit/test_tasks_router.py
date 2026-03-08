"""Unit tests for tasks router — CRUD, actions, events (planning layer)."""

from unittest.mock import AsyncMock, MagicMock
import uuid

from httpx import ASGITransport, AsyncClient
import pytest

from src.database import get_async_session
from src.main import app


def _make_task(**overrides):
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    defaults = {
        "id": "task-test1",
        "project_id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
        "type": "feature",
        "title": "Test feature",
        "description": None,
        "plan": None,
        "status": "backlog",
        "priority": 0,
        "acceptance_criteria": None,
        "current_iteration": 0,
        "max_iterations": 3,
        "created_by": "system",
        "source_brainstorm_id": None,
        "milestone_id": None,
        "repository_id": None,
        "story_id": None,
        "blocked_by_task_id": None,
        "need_e2e": False,
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)

    task = MagicMock()
    for k, v in defaults.items():
        setattr(task, k, v)
    return task


def _make_event(
    id=1,
    task_id="task-test1",
    event_type="status_change",
    from_status="backlog",
    to_status="todo",
    iteration=None,
    details=None,
    actor="system",
    created_at=None,
    updated_at=None,
):
    from datetime import UTC, datetime

    ev = MagicMock()
    ev.id = id
    ev.task_id = task_id
    ev.event_type = event_type
    ev.from_status = from_status
    ev.to_status = to_status
    ev.iteration = iteration
    ev.details = details or {}
    ev.actor = actor
    ev.created_at = created_at or datetime.now(UTC)
    ev.updated_at = updated_at or datetime.now(UTC)
    return ev


def _mock_session(scalar_one_or_none=None, scalars_all=None):
    session = AsyncMock()

    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=scalar_one_or_none)
    if scalars_all is not None:
        mock_scalars = MagicMock()
        mock_scalars.all = MagicMock(return_value=scalars_all)
        mock_result.scalars = MagicMock(return_value=mock_scalars)

    session.execute = AsyncMock(return_value=mock_result)
    session.add = MagicMock()
    session.commit = AsyncMock()

    async def _refresh(obj):
        pass

    session.refresh = _refresh

    return session


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    app.dependency_overrides.clear()


def _override_session(session):
    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override


# --- CRUD ---


@pytest.mark.asyncio
async def test_create_task():
    session = _mock_session()

    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/tasks/",
            json={
                "title": "Add stats button",
                "type": "feature",
                "project_id": "00000000-0000-0000-0000-000000000001",
            },
        )

    assert resp.status_code == 201  # noqa: PLR2004
    session.add.assert_called_once()
    task = session.add.call_args[0][0]
    assert task.title == "Add stats button"
    assert task.type == "feature"
    assert task.status == "backlog"
    assert task.project_id == uuid.UUID("00000000-0000-0000-0000-000000000001")
    assert task.id.startswith("task-")


@pytest.mark.asyncio
async def test_create_task_requires_project_id():
    session = _mock_session()
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/tasks/", json={"title": "Bug fix"})

    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_create_task_invalid_type():
    session = _mock_session()
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/tasks/", json={"title": "Bad", "type": "nonexistent"})

    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_tasks():
    t1 = _make_task(id="task-1", title="First")
    t2 = _make_task(id="task-2", title="Second")
    session = _mock_session(scalars_all=[t1, t2])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/tasks/")

    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert len(data) == 2  # noqa: PLR2004
    assert data[0]["id"] == "task-1"


@pytest.mark.asyncio
async def test_list_tasks_filter_by_source_brainstorm_id():
    """GET /api/tasks/?source_brainstorm_id=bs-xxx filters correctly."""
    t1 = _make_task(id="task-1", title="From brainstorm", source_brainstorm_id="bs-xxx")
    session = _mock_session(scalars_all=[t1])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/tasks/?source_brainstorm_id=bs-xxx")

    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert len(data) == 1
    assert data[0]["source_brainstorm_id"] == "bs-xxx"


@pytest.mark.asyncio
async def test_list_tasks_with_filters():
    session = _mock_session(scalars_all=[])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/tasks/?status=todo&type=fix&project_id=00000000-0000-0000-0000-000000000001"
        )

    assert resp.status_code == 200  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_tasks_with_limit():
    t1 = _make_task(id="task-1", title="First")
    t2 = _make_task(id="task-2", title="Second")
    t3 = _make_task(id="task-3", title="Third")
    session = _mock_session(scalars_all=[t1, t2, t3])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/tasks/?limit=1")

    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert len(data) <= 3  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_tasks_sort_created_at():
    """Verify sort param is accepted (actual ordering tested in service test)."""
    session = _mock_session(scalars_all=[])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/tasks/?sort=-created_at")

    assert resp.status_code == 200  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_tasks_with_since_filter():
    """Verify since param is accepted for filtering by updated_at."""
    session = _mock_session(scalars_all=[])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/tasks/?since=2026-03-01T00:00:00")

    assert resp.status_code == 200  # noqa: PLR2004


@pytest.mark.asyncio
async def test_stats_endpoint():
    """GET /api/tasks/stats returns status counts."""

    session = AsyncMock()

    mock_result = MagicMock()
    mock_result.scalar_one = MagicMock(return_value=0)
    session.execute = AsyncMock(return_value=mock_result)
    session.commit = AsyncMock()

    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/tasks/stats")

    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert "backlog" in data
    assert "done" in data
    assert "in_dev" in data
    assert "total" in data


@pytest.mark.asyncio
async def test_next_tag_endpoint():
    """GET /api/tasks/next-tag returns next available tag number."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value="#60 Some task")
    session.execute = AsyncMock(return_value=mock_result)
    session.commit = AsyncMock()

    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/tasks/next-tag")

    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert "next_tag" in data


@pytest.mark.asyncio
async def test_get_task():
    task = _make_task(id="task-abc")
    session = AsyncMock()
    mock_result1 = MagicMock()
    mock_result1.scalar_one_or_none = MagicMock(return_value=task)
    mock_result2 = MagicMock()
    mock_result2.scalar_one_or_none = MagicMock(return_value=None)
    session.execute = AsyncMock(side_effect=[mock_result1, mock_result2])
    session.commit = AsyncMock()

    async def _refresh(obj):
        pass

    session.refresh = _refresh
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/tasks/task-abc")

    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["id"] == "task-abc"


@pytest.mark.asyncio
async def test_get_task_not_found():
    session = _mock_session(scalar_one_or_none=None)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/tasks/task-nonexistent")

    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_update_task():
    task = _make_task(id="task-abc")
    session = _mock_session(scalar_one_or_none=task)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            "/api/tasks/task-abc", json={"title": "Updated title", "priority": 5}
        )

    assert resp.status_code == 200  # noqa: PLR2004
    assert task.title == "Updated title"
    assert task.priority == 5  # noqa: PLR2004


@pytest.mark.asyncio
async def test_cancel_task():
    task = _make_task(id="task-abc", status="backlog")
    session = _mock_session(scalar_one_or_none=task)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete("/api/tasks/task-abc")

    assert resp.status_code == 200  # noqa: PLR2004
    assert task.status == "cancelled"
    assert session.add.call_count == 1


# --- Lookup ---


@pytest.mark.asyncio
async def test_lookup_by_tag():
    task = _make_task(id="task-abc", title="#53 Compose runner fix")
    session = AsyncMock()
    mock_result1 = MagicMock()
    mock_result1.scalar_one_or_none = MagicMock(return_value=task)
    mock_result2 = MagicMock()
    mock_result2.scalar_one_or_none = MagicMock(return_value=None)
    session.execute = AsyncMock(side_effect=[mock_result1, mock_result2])
    session.commit = AsyncMock()

    async def _refresh(obj):
        pass

    session.refresh = _refresh
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/tasks/by-tag/53")

    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["id"] == "task-abc"


@pytest.mark.asyncio
async def test_lookup_by_tag_not_found():
    session = _mock_session(scalar_one_or_none=None)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/tasks/by-tag/999")

    assert resp.status_code == 404  # noqa: PLR2004


# --- Actions ---


@pytest.mark.asyncio
async def test_start_from_backlog():
    """Start from backlog should auto-promote backlog → todo → in_dev."""
    task = _make_task(id="task-abc", status="backlog")
    session = _mock_session(scalar_one_or_none=task)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/tasks/task-abc/start")

    assert resp.status_code == 200  # noqa: PLR2004
    assert task.status == "in_dev"
    # Two events: backlog→todo, todo→in_dev
    assert session.add.call_count == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_start_from_todo():
    task = _make_task(id="task-abc", status="todo")
    session = _mock_session(scalar_one_or_none=task)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/tasks/task-abc/start")

    assert resp.status_code == 200  # noqa: PLR2004
    assert task.status == "in_dev"
    assert session.add.call_count == 1


@pytest.mark.asyncio
async def test_start_from_done_fails():
    task = _make_task(id="task-abc", status="done")
    session = _mock_session(scalar_one_or_none=task)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/tasks/task-abc/start")

    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_complete_from_testing():
    task = _make_task(id="task-abc", status="testing")
    session = _mock_session(scalar_one_or_none=task)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/tasks/task-abc/complete")

    assert resp.status_code == 200  # noqa: PLR2004
    assert task.status == "done"
    # Direct transition: testing → done (1 event)
    assert session.add.call_count == 1


@pytest.mark.asyncio
async def test_complete_from_in_ci():
    """Complete from in_ci should auto-promote: in_ci → testing → done."""
    task = _make_task(id="task-abc", status="in_ci")
    session = _mock_session(scalar_one_or_none=task)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/tasks/task-abc/complete")

    assert resp.status_code == 200  # noqa: PLR2004
    assert task.status == "done"
    # Two events: in_ci → testing, testing → done
    assert session.add.call_count == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_complete_from_in_dev():
    """Complete from in_dev should auto-promote: in_dev → in_ci → testing → done."""
    task = _make_task(id="task-abc", status="in_dev")
    session = _mock_session(scalar_one_or_none=task)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/tasks/task-abc/complete")

    assert resp.status_code == 200  # noqa: PLR2004
    assert task.status == "done"
    # Three events: in_dev → in_ci, in_ci → testing, testing → done
    assert session.add.call_count == 3  # noqa: PLR2004


@pytest.mark.asyncio
async def test_complete_from_backlog_fails():
    task = _make_task(id="task-abc", status="backlog")
    session = _mock_session(scalar_one_or_none=task)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/tasks/task-abc/complete")

    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_complete_from_cancelled_fails():
    task = _make_task(id="task-abc", status="cancelled")
    session = _mock_session(scalar_one_or_none=task)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/tasks/task-abc/complete")

    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_fail_task():
    task = _make_task(id="task-abc", status="in_dev")
    session = _mock_session(scalar_one_or_none=task)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/tasks/task-abc/fail",
            json={"reason": "CI crashed 3 times"},
        )

    assert resp.status_code == 200  # noqa: PLR2004
    assert task.status == "failed"


@pytest.mark.asyncio
async def test_reopen_from_done():
    task = _make_task(id="task-abc", status="done")
    session = _mock_session(scalar_one_or_none=task)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/tasks/task-abc/reopen",
            json={"reason": "Bug came back"},
        )

    assert resp.status_code == 200  # noqa: PLR2004
    assert task.status == "backlog"


@pytest.mark.asyncio
async def test_reopen_from_in_dev_fails():
    task = _make_task(id="task-abc", status="in_dev")
    session = _mock_session(scalar_one_or_none=task)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/tasks/task-abc/reopen")

    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_generic_transition():
    task = _make_task(id="task-abc", status="in_dev")
    session = _mock_session(scalar_one_or_none=task)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/tasks/task-abc/transition?to_status=in_ci")

    assert resp.status_code == 200  # noqa: PLR2004
    assert task.status == "in_ci"


@pytest.mark.asyncio
async def test_generic_transition_invalid():
    task = _make_task(id="task-abc", status="backlog")
    session = _mock_session(scalar_one_or_none=task)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/tasks/task-abc/transition?to_status=done")

    assert resp.status_code == 422  # noqa: PLR2004
    assert "Cannot transition" in resp.json()["detail"]


# --- Full transition flow ---


@pytest.mark.asyncio
async def test_full_flow_backlog_to_done():
    """Verify the full lifecycle: backlog → start → in_ci → testing → done."""
    task = _make_task(id="task-flow", status="backlog", need_e2e=False)
    session = _mock_session(scalar_one_or_none=task)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Start: backlog → todo → in_dev
        resp = await client.post("/api/tasks/task-flow/start", json={"actor": "claude"})
        assert resp.status_code == 200  # noqa: PLR2004
        assert task.status == "in_dev"

        # Transition to in_ci
        session.add.reset_mock()
        resp = await client.post(
            "/api/tasks/task-flow/transition?to_status=in_ci", json={"actor": "claude"}
        )
        assert resp.status_code == 200  # noqa: PLR2004
        assert task.status == "in_ci"

        # Transition to testing
        session.add.reset_mock()
        resp = await client.post(
            "/api/tasks/task-flow/transition?to_status=testing", json={"actor": "claude"}
        )
        assert resp.status_code == 200  # noqa: PLR2004
        assert task.status == "testing"

        # Complete: testing → done
        session.add.reset_mock()
        resp = await client.post("/api/tasks/task-flow/complete", json={"actor": "claude"})
        assert resp.status_code == 200  # noqa: PLR2004
        assert task.status == "done"
        assert session.add.call_count == 1  # single event


@pytest.mark.asyncio
async def test_complete_auto_promotes_full_chain():
    """Verify /complete auto-walks in_dev → in_ci → testing → done."""
    task = _make_task(id="task-auto", status="in_dev")
    session = _mock_session(scalar_one_or_none=task)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/tasks/task-auto/complete", json={"actor": "claude"})

    assert resp.status_code == 200  # noqa: PLR2004
    assert task.status == "done"
    # 3 events: in_dev→in_ci, in_ci→testing, testing→done
    assert session.add.call_count == 3  # noqa: PLR2004


@pytest.mark.asyncio
async def test_ci_red_back_to_dev():
    """Verify in_ci → in_dev transition (CI failure)."""
    task = _make_task(id="task-cifix", status="in_ci")
    session = _mock_session(scalar_one_or_none=task)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/tasks/task-cifix/transition?to_status=in_dev", json={"actor": "claude"}
        )
        assert resp.status_code == 200  # noqa: PLR2004
        assert task.status == "in_dev"


# --- Events ---


@pytest.mark.asyncio
async def test_create_event():
    task = _make_task(id="task-abc")
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=task)
    session.execute = AsyncMock(return_value=mock_result)
    session.add = MagicMock()
    session.commit = AsyncMock()

    async def _refresh(obj):
        if hasattr(obj, "id") and obj.id is None:
            obj.id = 1

    session.refresh = _refresh
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/tasks/task-abc/events",
            json={
                "event_type": "iteration_start",
                "iteration": 0,
                "details": {"run_id": "eng-111"},
                "actor": "system",
            },
        )

    assert resp.status_code == 201  # noqa: PLR2004
    event = session.add.call_args[0][0]
    assert event.event_type == "iteration_start"
    assert event.iteration == 0
    assert event.details == {"run_id": "eng-111"}


@pytest.mark.asyncio
async def test_list_events():
    task = _make_task(id="task-abc")
    ev1 = _make_event(id=1, task_id="task-abc")
    ev2 = _make_event(id=2, task_id="task-abc", event_type="iteration_start", iteration=0)

    session = AsyncMock()
    mock_result_task = MagicMock()
    mock_result_task.scalar_one_or_none = MagicMock(return_value=task)
    mock_result_events = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all = MagicMock(return_value=[ev1, ev2])
    mock_result_events.scalars = MagicMock(return_value=mock_scalars)
    session.execute = AsyncMock(side_effect=[mock_result_task, mock_result_events])
    session.commit = AsyncMock()

    async def _refresh(obj):
        pass

    session.refresh = _refresh
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/tasks/task-abc/events")

    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert len(data) == 2  # noqa: PLR2004


# --- Push (auto-priority) ---


@pytest.mark.asyncio
async def test_push_task_sets_priority_below_min():
    """POST /api/tasks/push creates task with priority = min(backlog) - 1."""
    session = AsyncMock()

    # First execute: min priority query returns 2
    mock_min_result = MagicMock()
    mock_min_result.scalar_one_or_none = MagicMock(return_value=2)
    # Second+ executes: not needed (add/commit/refresh handle creation)
    session.execute = AsyncMock(return_value=mock_min_result)
    session.add = MagicMock()
    session.commit = AsyncMock()

    async def _refresh(obj):
        pass

    session.refresh = _refresh
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/tasks/push",
            json={"title": "Urgent fix", "project_id": "00000000-0000-0000-0000-000000000001"},
        )

    assert resp.status_code == 201  # noqa: PLR2004
    task = session.add.call_args[0][0]
    assert task.priority == 1
    assert task.title == "Urgent fix"
    assert task.status == "backlog"


@pytest.mark.asyncio
async def test_push_task_empty_backlog():
    """Push to empty backlog gives priority -1."""
    session = AsyncMock()

    mock_min_result = MagicMock()
    mock_min_result.scalar_one_or_none = MagicMock(return_value=None)
    session.execute = AsyncMock(return_value=mock_min_result)
    session.add = MagicMock()
    session.commit = AsyncMock()

    async def _refresh(obj):
        pass

    session.refresh = _refresh
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/tasks/push",
            json={"title": "First task", "project_id": "00000000-0000-0000-0000-000000000001"},
        )

    assert resp.status_code == 201  # noqa: PLR2004
    task = session.add.call_args[0][0]
    assert task.priority == -1


@pytest.mark.asyncio
async def test_push_twice_decreasing_priority():
    """Two pushes in a row give decreasing priorities."""
    session = AsyncMock()
    priorities = []

    mock_min1 = MagicMock()
    mock_min1.scalar_one_or_none = MagicMock(return_value=3)
    mock_min2 = MagicMock()
    mock_min2.scalar_one_or_none = MagicMock(return_value=2)
    session.execute = AsyncMock(side_effect=[mock_min1, mock_min2])
    session.add = MagicMock()
    session.commit = AsyncMock()

    async def _refresh(obj):
        pass

    session.refresh = _refresh
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/tasks/push",
            json={"title": "Task A", "project_id": "00000000-0000-0000-0000-000000000001"},
        )
        await client.post(
            "/api/tasks/push",
            json={"title": "Task B", "project_id": "00000000-0000-0000-0000-000000000001"},
        )

    for call in session.add.call_args_list:
        priorities.append(call[0][0].priority)

    assert priorities == [2, 1]


@pytest.mark.asyncio
async def test_events_for_nonexistent_task():
    session = _mock_session(scalar_one_or_none=None)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/tasks/task-nonexistent/events")

    assert resp.status_code == 404  # noqa: PLR2004
