"""Unit tests for milestones router — CRUD + action endpoints."""

from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from src.database import get_async_session
from src.main import app


def _make_milestone(**overrides):
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    defaults = {
        "id": "ms-test1",
        "project_id": "proj-test",
        "title": "Test milestone",
        "description": None,
        "sort_order": 0,
        "status": "open",
        "parent_id": None,
        "created_by": "system",
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)

    ms = MagicMock()
    for k, v in defaults.items():
        setattr(ms, k, v)
    return ms


def _make_work_item(**overrides):
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    defaults = {
        "id": "wi-test1",
        "project_id": "proj-test",
        "type": "feature",
        "title": "#99 Test task",
        "description": None,
        "plan": None,
        "status": "backlog",
        "priority": 0,
        "acceptance_criteria": None,
        "current_iteration": 0,
        "max_iterations": 3,
        "created_by": "system",
        "source_brainstorm_id": None,
        "milestone_id": "ms-test1",
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)

    wi = MagicMock()
    for k, v in defaults.items():
        setattr(wi, k, v)
    return wi


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
async def test_create_milestone():
    session = _mock_session()
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/milestones/",
            json={"title": "Phase 1", "project_id": "proj-1"},
        )

    assert resp.status_code == 201
    session.add.assert_called_once()
    ms = session.add.call_args[0][0]
    assert ms.title == "Phase 1"
    assert ms.status == "open"
    assert ms.project_id == "proj-1"
    assert ms.id.startswith("ms-")


@pytest.mark.asyncio
async def test_create_milestone_requires_project_id():
    session = _mock_session()
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/milestones/", json={"title": "No project"})

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_milestones():
    ms1 = _make_milestone(id="ms-1", title="Phase 1")
    ms2 = _make_milestone(id="ms-2", title="Phase 2")
    session = _mock_session(scalars_all=[ms1, ms2])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/milestones/")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2


@pytest.mark.asyncio
async def test_list_milestones_with_filters():
    session = _mock_session(scalars_all=[])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/milestones/?status=open&project_id=proj-1")

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_milestone():
    ms = _make_milestone(id="ms-abc")
    session = _mock_session(scalar_one_or_none=ms)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/milestones/ms-abc")

    assert resp.status_code == 200
    assert resp.json()["id"] == "ms-abc"


@pytest.mark.asyncio
async def test_get_milestone_not_found():
    session = _mock_session(scalar_one_or_none=None)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/milestones/ms-nonexistent")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_milestone():
    ms = _make_milestone(id="ms-abc")
    session = _mock_session(scalar_one_or_none=ms)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            "/api/milestones/ms-abc",
            json={"title": "Updated Phase 1", "sort_order": 5},
        )

    assert resp.status_code == 200
    assert ms.title == "Updated Phase 1"
    assert ms.sort_order == 5


@pytest.mark.asyncio
async def test_delete_milestone():
    ms = _make_milestone(id="ms-abc", status="open")
    session = _mock_session(scalar_one_or_none=ms)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete("/api/milestones/ms-abc")

    assert resp.status_code == 200


# --- Action endpoints ---


@pytest.mark.asyncio
async def test_complete_from_open():
    ms = _make_milestone(id="ms-abc", status="open")
    session = _mock_session(scalar_one_or_none=ms)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/milestones/ms-abc/complete")

    assert resp.status_code == 200
    assert ms.status == "completed"


@pytest.mark.asyncio
async def test_complete_from_completed_fails():
    ms = _make_milestone(id="ms-abc", status="completed")
    session = _mock_session(scalar_one_or_none=ms)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/milestones/ms-abc/complete")

    assert resp.status_code == 409


# --- Work items sub-resource ---


@pytest.mark.asyncio
async def test_get_milestone_work_items():
    ms = _make_milestone(id="ms-abc")
    wi1 = _make_work_item(id="wi-1", milestone_id="ms-abc")
    wi2 = _make_work_item(id="wi-2", milestone_id="ms-abc", status="done")

    # First execute returns milestone, second returns work items
    session = AsyncMock()
    result1 = MagicMock()
    result1.scalar_one_or_none = MagicMock(return_value=ms)
    result2 = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all = MagicMock(return_value=[wi1, wi2])
    result2.scalars = MagicMock(return_value=mock_scalars)
    session.execute = AsyncMock(side_effect=[result1, result2])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/milestones/ms-abc/work-items")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
