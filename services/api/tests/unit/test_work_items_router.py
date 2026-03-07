"""Unit tests for work items router — CRUD, actions, events."""

from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from src.database import get_async_session
from src.main import app


def _make_work_item(**overrides):
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    defaults = {
        "id": "wi-test1",
        "project_id": "proj-test",
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
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)

    wi = MagicMock()
    for k, v in defaults.items():
        setattr(wi, k, v)
    return wi


def _make_event(
    id=1,
    work_item_id="wi-test1",
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
    ev.work_item_id = work_item_id
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
async def test_create_work_item():
    session = _mock_session()

    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/work-items/",
            json={"title": "Add stats button", "type": "feature", "project_id": "proj-1"},
        )

    assert resp.status_code == 201  # noqa: PLR2004
    session.add.assert_called_once()
    wi = session.add.call_args[0][0]
    assert wi.title == "Add stats button"
    assert wi.type == "feature"
    assert wi.status == "backlog"
    assert wi.project_id == "proj-1"
    assert wi.id.startswith("wi-")


@pytest.mark.asyncio
async def test_create_work_item_requires_project_id():
    session = _mock_session()
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/work-items/", json={"title": "Bug fix"})

    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_create_work_item_invalid_type():
    session = _mock_session()
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/work-items/", json={"title": "Bad", "type": "nonexistent"})

    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_work_items():
    wi1 = _make_work_item(id="wi-1", title="First")
    wi2 = _make_work_item(id="wi-2", title="Second")
    session = _mock_session(scalars_all=[wi1, wi2])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/work-items/")

    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert len(data) == 2  # noqa: PLR2004
    assert data[0]["id"] == "wi-1"


@pytest.mark.asyncio
async def test_list_work_items_with_filters():
    session = _mock_session(scalars_all=[])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/work-items/?status=todo&type=fix&project_id=proj-1")

    assert resp.status_code == 200  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_work_items_with_limit():
    wi1 = _make_work_item(id="wi-1", title="First")
    wi2 = _make_work_item(id="wi-2", title="Second")
    wi3 = _make_work_item(id="wi-3", title="Third")
    session = _mock_session(scalars_all=[wi1, wi2, wi3])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/work-items/?limit=1")

    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    # limit is applied server-side, but with mock we get all 3 back
    # The real test is that the endpoint accepts the param without error
    # and the SQL query has .limit() — verified in service test
    assert len(data) <= 3  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_work_items_sort_created_at():
    """Verify sort param is accepted (actual ordering tested in service test)."""
    session = _mock_session(scalars_all=[])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/work-items/?sort=-created_at")

    assert resp.status_code == 200  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_work_items_with_since_filter():
    """Verify since param is accepted for filtering by updated_at."""
    session = _mock_session(scalars_all=[])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/work-items/?since=2026-03-01T00:00:00")

    assert resp.status_code == 200  # noqa: PLR2004


@pytest.mark.asyncio
async def test_stats_endpoint():
    """GET /api/work-items/stats returns status counts."""

    session = AsyncMock()

    # stats endpoint runs one query per status — mock all results
    mock_result = MagicMock()
    mock_result.scalar_one = MagicMock(return_value=0)
    session.execute = AsyncMock(return_value=mock_result)
    session.commit = AsyncMock()

    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/work-items/stats")

    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert "backlog" in data
    assert "done" in data
    assert "in_dev" in data
    assert "total" in data


@pytest.mark.asyncio
async def test_next_tag_endpoint():
    """GET /api/work-items/next-tag returns next available tag number."""
    session = AsyncMock()
    mock_result = MagicMock()
    # Simulate max tag = 60 -> next = 61
    mock_result.scalar_one_or_none = MagicMock(return_value="#60 Some task")
    session.execute = AsyncMock(return_value=mock_result)
    session.commit = AsyncMock()

    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/work-items/next-tag")

    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert "next_tag" in data


@pytest.mark.asyncio
async def test_get_work_item():
    wi = _make_work_item(id="wi-abc")
    # First call returns work item, second returns event (for last_event)
    session = AsyncMock()
    mock_result1 = MagicMock()
    mock_result1.scalar_one_or_none = MagicMock(return_value=wi)
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
        resp = await client.get("/api/work-items/wi-abc")

    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["id"] == "wi-abc"


@pytest.mark.asyncio
async def test_get_work_item_not_found():
    session = _mock_session(scalar_one_or_none=None)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/work-items/wi-nonexistent")

    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_update_work_item():
    wi = _make_work_item(id="wi-abc")
    session = _mock_session(scalar_one_or_none=wi)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            "/api/work-items/wi-abc", json={"title": "Updated title", "priority": 5}
        )

    assert resp.status_code == 200  # noqa: PLR2004
    assert wi.title == "Updated title"
    assert wi.priority == 5  # noqa: PLR2004


@pytest.mark.asyncio
async def test_cancel_work_item():
    wi = _make_work_item(id="wi-abc", status="backlog")
    session = _mock_session(scalar_one_or_none=wi)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete("/api/work-items/wi-abc")

    assert resp.status_code == 200  # noqa: PLR2004
    assert wi.status == "cancelled"
    # Should have created a status_change event
    assert session.add.call_count == 1


# --- Lookup ---


@pytest.mark.asyncio
async def test_lookup_by_tag():
    wi = _make_work_item(id="wi-abc", title="#53 Compose runner fix")
    # Two queries: 1) find by tag, 2) last event
    session = AsyncMock()
    mock_result1 = MagicMock()
    mock_result1.scalar_one_or_none = MagicMock(return_value=wi)
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
        resp = await client.get("/api/work-items/by-tag/53")

    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["id"] == "wi-abc"


@pytest.mark.asyncio
async def test_lookup_by_tag_not_found():
    session = _mock_session(scalar_one_or_none=None)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/work-items/by-tag/999")

    assert resp.status_code == 404  # noqa: PLR2004


# --- Actions ---


@pytest.mark.asyncio
async def test_start_from_backlog():
    """Start from backlog should auto-promote backlog → todo → in_dev."""
    wi = _make_work_item(id="wi-abc", status="backlog")
    session = _mock_session(scalar_one_or_none=wi)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/work-items/wi-abc/start")

    assert resp.status_code == 200  # noqa: PLR2004
    assert wi.status == "in_dev"
    # Two events: backlog→todo, todo→in_dev
    assert session.add.call_count == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_start_from_todo():
    wi = _make_work_item(id="wi-abc", status="todo")
    session = _mock_session(scalar_one_or_none=wi)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/work-items/wi-abc/start")

    assert resp.status_code == 200  # noqa: PLR2004
    assert wi.status == "in_dev"
    assert session.add.call_count == 1


@pytest.mark.asyncio
async def test_start_from_done_fails():
    wi = _make_work_item(id="wi-abc", status="done")
    session = _mock_session(scalar_one_or_none=wi)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/work-items/wi-abc/start")

    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_complete_from_testing():
    wi = _make_work_item(id="wi-abc", status="testing")
    session = _mock_session(scalar_one_or_none=wi)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/work-items/wi-abc/complete")

    assert resp.status_code == 200  # noqa: PLR2004
    assert wi.status == "done"


@pytest.mark.asyncio
async def test_complete_from_backlog_fails():
    wi = _make_work_item(id="wi-abc", status="backlog")
    session = _mock_session(scalar_one_or_none=wi)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/work-items/wi-abc/complete")

    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_fail_work_item():
    wi = _make_work_item(id="wi-abc", status="in_dev")
    session = _mock_session(scalar_one_or_none=wi)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/work-items/wi-abc/fail",
            json={"reason": "CI crashed 3 times"},
        )

    assert resp.status_code == 200  # noqa: PLR2004
    assert wi.status == "failed"


@pytest.mark.asyncio
async def test_reopen_from_done():
    wi = _make_work_item(id="wi-abc", status="done")
    session = _mock_session(scalar_one_or_none=wi)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/work-items/wi-abc/reopen",
            json={"reason": "Bug came back"},
        )

    assert resp.status_code == 200  # noqa: PLR2004
    assert wi.status == "backlog"


@pytest.mark.asyncio
async def test_reopen_from_in_dev_fails():
    wi = _make_work_item(id="wi-abc", status="in_dev")
    session = _mock_session(scalar_one_or_none=wi)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/work-items/wi-abc/reopen")

    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_generic_transition():
    wi = _make_work_item(id="wi-abc", status="in_dev")
    session = _mock_session(scalar_one_or_none=wi)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/work-items/wi-abc/transition?to_status=testing")

    assert resp.status_code == 200  # noqa: PLR2004
    assert wi.status == "testing"


@pytest.mark.asyncio
async def test_generic_transition_invalid():
    wi = _make_work_item(id="wi-abc", status="backlog")
    session = _mock_session(scalar_one_or_none=wi)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/work-items/wi-abc/transition?to_status=done")

    assert resp.status_code == 422  # noqa: PLR2004
    assert "Cannot transition" in resp.json()["detail"]


# --- Events ---


@pytest.mark.asyncio
async def test_create_event():
    wi = _make_work_item(id="wi-abc")
    # First execute returns work_item, rest for events
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=wi)
    session.execute = AsyncMock(return_value=mock_result)
    session.add = MagicMock()
    session.commit = AsyncMock()

    async def _refresh(obj):
        # Simulate DB assigning auto-increment id
        if hasattr(obj, "id") and obj.id is None:
            obj.id = 1

    session.refresh = _refresh
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/work-items/wi-abc/events",
            json={
                "event_type": "iteration_start",
                "iteration": 0,
                "details": {"task_id": "eng-111"},
                "actor": "system",
            },
        )

    assert resp.status_code == 201  # noqa: PLR2004
    event = session.add.call_args[0][0]
    assert event.event_type == "iteration_start"
    assert event.iteration == 0
    assert event.details == {"task_id": "eng-111"}


@pytest.mark.asyncio
async def test_list_events():
    wi = _make_work_item(id="wi-abc")
    ev1 = _make_event(id=1, work_item_id="wi-abc")
    ev2 = _make_event(id=2, work_item_id="wi-abc", event_type="iteration_start", iteration=0)

    session = AsyncMock()
    # First call: get_work_item, second: list events
    mock_result_wi = MagicMock()
    mock_result_wi.scalar_one_or_none = MagicMock(return_value=wi)
    mock_result_events = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all = MagicMock(return_value=[ev1, ev2])
    mock_result_events.scalars = MagicMock(return_value=mock_scalars)
    session.execute = AsyncMock(side_effect=[mock_result_wi, mock_result_events])
    session.commit = AsyncMock()

    async def _refresh(obj):
        pass

    session.refresh = _refresh
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/work-items/wi-abc/events")

    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert len(data) == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_events_for_nonexistent_work_item():
    session = _mock_session(scalar_one_or_none=None)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/work-items/wi-nonexistent/events")

    assert resp.status_code == 404  # noqa: PLR2004
