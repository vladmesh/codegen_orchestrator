"""Unit tests for brainstorms router — CRUD + action endpoints."""

from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from src.database import get_async_session
from src.main import app


def _make_brainstorm(**overrides):
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    defaults = {
        "id": "bs-test1",
        "project_id": "proj-test",
        "title": "Test brainstorm",
        "content": None,
        "status": "draft",
        "created_by": "system",
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)

    bs = MagicMock()
    for k, v in defaults.items():
        setattr(bs, k, v)
    return bs


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
async def test_create_brainstorm():
    session = _mock_session()
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/brainstorms/",
            json={"title": "Worker isolation", "project_id": "proj-1"},
        )

    assert resp.status_code == 201
    session.add.assert_called_once()
    bs = session.add.call_args[0][0]
    assert bs.title == "Worker isolation"
    assert bs.status == "draft"
    assert bs.project_id == "proj-1"
    assert bs.id.startswith("bs-")


@pytest.mark.asyncio
async def test_create_brainstorm_requires_project_id():
    session = _mock_session()
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/brainstorms/", json={"title": "No project"})

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_brainstorms():
    bs1 = _make_brainstorm(id="bs-1", title="First")
    bs2 = _make_brainstorm(id="bs-2", title="Second")
    session = _mock_session(scalars_all=[bs1, bs2])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/brainstorms/")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2


@pytest.mark.asyncio
async def test_list_brainstorms_with_filters():
    session = _mock_session(scalars_all=[])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/brainstorms/?status=done&project_id=proj-1")

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_brainstorm():
    bs = _make_brainstorm(id="bs-abc")
    session = _mock_session(scalar_one_or_none=bs)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/brainstorms/bs-abc")

    assert resp.status_code == 200
    assert resp.json()["id"] == "bs-abc"


@pytest.mark.asyncio
async def test_get_brainstorm_not_found():
    session = _mock_session(scalar_one_or_none=None)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/brainstorms/bs-nonexistent")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_brainstorm():
    bs = _make_brainstorm(id="bs-abc")
    session = _mock_session(scalar_one_or_none=bs)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            "/api/brainstorms/bs-abc",
            json={"title": "Updated title", "content": "New analysis"},
        )

    assert resp.status_code == 200
    assert bs.title == "Updated title"
    assert bs.content == "New analysis"


@pytest.mark.asyncio
async def test_delete_brainstorm():
    bs = _make_brainstorm(id="bs-abc", status="draft")
    session = _mock_session(scalar_one_or_none=bs)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete("/api/brainstorms/bs-abc")

    assert resp.status_code == 200
    assert bs.status == "archived"


# --- Action endpoints ---


@pytest.mark.asyncio
async def test_done_from_draft():
    bs = _make_brainstorm(id="bs-abc", status="draft")
    session = _mock_session(scalar_one_or_none=bs)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/brainstorms/bs-abc/done")

    assert resp.status_code == 200
    assert bs.status == "done"


@pytest.mark.asyncio
async def test_done_from_done_fails():
    bs = _make_brainstorm(id="bs-abc", status="done")
    session = _mock_session(scalar_one_or_none=bs)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/brainstorms/bs-abc/done")

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_triage_from_done():
    bs = _make_brainstorm(id="bs-abc", status="done")
    session = _mock_session(scalar_one_or_none=bs)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/brainstorms/bs-abc/triage")

    assert resp.status_code == 200
    assert bs.status == "triaged"


@pytest.mark.asyncio
async def test_triage_from_draft_fails():
    bs = _make_brainstorm(id="bs-abc", status="draft")
    session = _mock_session(scalar_one_or_none=bs)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/brainstorms/bs-abc/triage")

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_archive_from_triaged():
    bs = _make_brainstorm(id="bs-abc", status="triaged")
    session = _mock_session(scalar_one_or_none=bs)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/brainstorms/bs-abc/archive")

    assert resp.status_code == 200
    assert bs.status == "archived"


@pytest.mark.asyncio
async def test_archive_from_done():
    bs = _make_brainstorm(id="bs-abc", status="done")
    session = _mock_session(scalar_one_or_none=bs)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/brainstorms/bs-abc/archive")

    assert resp.status_code == 200
    assert bs.status == "archived"


@pytest.mark.asyncio
async def test_archive_from_draft_fails():
    bs = _make_brainstorm(id="bs-abc", status="draft")
    session = _mock_session(scalar_one_or_none=bs)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/brainstorms/bs-abc/archive")

    assert resp.status_code == 409
