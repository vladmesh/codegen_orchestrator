"""Unit tests for stories router — CRUD + action-based status transitions."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
import uuid

from httpx import ASGITransport, AsyncClient
import pytest

from src.database import get_async_session
from src.main import app


def _make_story(**overrides):
    now = datetime.now(UTC)
    defaults = {
        "id": "story-test1",
        "project_id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
        "parent_story_id": None,
        "title": "Test story",
        "description": None,
        "acceptance_criteria": None,
        "status": "created",
        "priority": 0,
        "blocked_by_story_id": None,
        "created_by": "system",
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)

    story = MagicMock()
    for k, v in defaults.items():
        setattr(story, k, v)
    return story


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
async def test_create_story():
    session = _mock_session()
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/stories/",
            json={"title": "User login", "project_id": "00000000-0000-0000-0000-000000000001"},
        )

    assert resp.status_code == 201  # noqa: PLR2004
    session.add.assert_called_once()
    story = session.add.call_args[0][0]
    assert story.title == "User login"
    assert story.status == "created"
    assert story.project_id == uuid.UUID("00000000-0000-0000-0000-000000000001")
    assert story.id.startswith("story-")


@pytest.mark.asyncio
async def test_create_story_with_priority():
    session = _mock_session()
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/stories/",
            json={
                "title": "High prio",
                "project_id": "00000000-0000-0000-0000-000000000001",
                "priority": 5,
            },
        )

    assert resp.status_code == 201  # noqa: PLR2004
    story = session.add.call_args[0][0]
    assert story.priority == 5


@pytest.mark.asyncio
async def test_create_story_with_blocked_by():
    session = _mock_session()
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/stories/",
            json={
                "title": "Blocked",
                "project_id": "00000000-0000-0000-0000-000000000001",
                "blocked_by_story_id": "story-dep",
            },
        )

    assert resp.status_code == 201  # noqa: PLR2004
    story = session.add.call_args[0][0]
    assert story.blocked_by_story_id == "story-dep"


@pytest.mark.asyncio
async def test_create_story_requires_project_id():
    session = _mock_session()
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/stories/", json={"title": "No project"})

    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_stories():
    s1 = _make_story(id="story-1", title="First")
    s2 = _make_story(id="story-2", title="Second")
    session = _mock_session(scalars_all=[s1, s2])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/stories/")

    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert len(data) == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_stories_filter_by_project():
    s1 = _make_story(id="story-1", project_id=uuid.UUID("00000000-0000-0000-0000-000000000001"))
    session = _mock_session(scalars_all=[s1])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/stories/?project_id=00000000-0000-0000-0000-000000000001")

    assert resp.status_code == 200  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_stories_filter_by_status():
    session = _mock_session(scalars_all=[])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/stories/?status=in_progress")

    assert resp.status_code == 200  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_stories_filter_by_parent():
    session = _mock_session(scalars_all=[])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/stories/?parent_story_id=story-epic")

    assert resp.status_code == 200  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_story():
    story = _make_story(id="story-abc")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/stories/story-abc")

    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["id"] == "story-abc"


@pytest.mark.asyncio
async def test_get_story_not_found():
    session = _mock_session(scalar_one_or_none=None)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/stories/story-nonexistent")

    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_update_story():
    story = _make_story(id="story-abc")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch("/api/stories/story-abc", json={"title": "Updated title"})

    assert resp.status_code == 200  # noqa: PLR2004
    assert story.title == "Updated title"


# --- Action endpoints (status transitions) ---


@pytest.mark.asyncio
async def test_start_story():
    story = _make_story(id="story-abc", status="created")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/stories/story-abc/start")

    assert resp.status_code == 200  # noqa: PLR2004
    assert story.status == "in_progress"


@pytest.mark.asyncio
async def test_start_story_invalid_transition():
    story = _make_story(id="story-abc", status="archived")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/stories/story-abc/start")

    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_complete_story():
    story = _make_story(id="story-abc", status="in_progress")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/stories/story-abc/complete")

    assert resp.status_code == 200  # noqa: PLR2004
    assert story.status == "completed"


@pytest.mark.asyncio
async def test_complete_story_invalid_transition():
    story = _make_story(id="story-abc", status="created")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/stories/story-abc/complete")

    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_archive_story():
    story = _make_story(id="story-abc", status="completed")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/stories/story-abc/archive")

    assert resp.status_code == 200  # noqa: PLR2004
    assert story.status == "archived"


@pytest.mark.asyncio
async def test_archive_from_created():
    """Stories can be archived directly from created status."""
    story = _make_story(id="story-abc", status="created")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/stories/story-abc/archive")

    assert resp.status_code == 200  # noqa: PLR2004
    assert story.status == "archived"


# --- Priority filter + sort ---


@pytest.mark.asyncio
async def test_list_stories_filter_by_priority():
    session = _mock_session(scalars_all=[])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/stories/?priority=3")

    assert resp.status_code == 200  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_stories_with_sort():
    session = _mock_session(scalars_all=[])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/stories/?sort=-created_at")

    assert resp.status_code == 200  # noqa: PLR2004


# --- Blocked-by validation ---


@pytest.mark.asyncio
async def test_start_story_blocked_by_incomplete():
    """Cannot start a story whose blocker is not completed."""
    blocker = _make_story(id="story-blocker", status="in_progress")
    story = _make_story(id="story-abc", status="created", blocked_by_story_id="story-blocker")

    call_count = 0
    mock_result_story = MagicMock()
    mock_result_story.scalar_one_or_none = MagicMock(return_value=story)
    mock_result_blocker = MagicMock()
    mock_result_blocker.scalar_one_or_none = MagicMock(return_value=blocker)

    session = AsyncMock()

    async def _execute_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return mock_result_story
        return mock_result_blocker

    session.execute = AsyncMock(side_effect=_execute_side_effect)

    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/stories/story-abc/start")

    assert resp.status_code == 422  # noqa: PLR2004
    assert "blocked by story" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_start_story_blocked_by_completed():
    """Can start a story whose blocker is completed."""
    blocker = _make_story(id="story-blocker", status="completed")
    story = _make_story(id="story-abc", status="created", blocked_by_story_id="story-blocker")

    call_count = 0
    mock_result_story = MagicMock()
    mock_result_story.scalar_one_or_none = MagicMock(return_value=story)
    mock_result_blocker = MagicMock()
    mock_result_blocker.scalar_one_or_none = MagicMock(return_value=blocker)

    session = AsyncMock()

    async def _execute_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return mock_result_story
        return mock_result_blocker

    session.execute = AsyncMock(side_effect=_execute_side_effect)
    session.commit = AsyncMock()

    async def _refresh(obj):
        pass

    session.refresh = _refresh

    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/stories/story-abc/start")

    assert resp.status_code == 200  # noqa: PLR2004
    assert story.status == "in_progress"


@pytest.mark.asyncio
async def test_start_story_no_blocker():
    """Can start a story with no blocked_by set."""
    story = _make_story(id="story-abc", status="created", blocked_by_story_id=None)
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/stories/story-abc/start")

    assert resp.status_code == 200  # noqa: PLR2004
    assert story.status == "in_progress"
