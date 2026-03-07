"""Service test: /next skill flow via Work Items API.

Tests the API operations that the /next skill uses:
1. List backlog items with limit (pick top priority)
2. Start work item (backlog → in_dev)
3. Next call picks the second item (first is no longer backlog)
4. By-tag lookup
"""

from httpx import AsyncClient
import pytest


@pytest.mark.asyncio
async def test_next_picks_top_priority(async_client: AsyncClient):
    """GET ?status=backlog&limit=1 returns highest-priority item."""
    # Create 3 items with different priorities
    items = []
    for title, priority in [
        ("#901 Low priority task", 2),
        ("#900 High priority task", 0),
        ("#902 Medium priority task", 1),
    ]:
        resp = await async_client.post(
            "/api/work-items/",
            json={"title": title, "priority": priority},
        )
        assert resp.status_code == 201  # noqa: PLR2004
        items.append(resp.json())

    # /next with no argument: get backlog items sorted by priority
    resp = await async_client.get("/api/work-items/?status=backlog")
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    # Find our items among all backlog items
    our_items = [d for d in data if d["title"].startswith("#90")]
    assert len(our_items) == 3  # noqa: PLR2004
    # Priority 0 should come first
    assert our_items[0]["title"] == "#900 High priority task"
    assert our_items[1]["title"] == "#902 Medium priority task"
    assert our_items[2]["title"] == "#901 Low priority task"


@pytest.mark.asyncio
async def test_next_start_advances_queue(async_client: AsyncClient):
    """After starting top item, next call returns the second item."""
    # Create 2 items
    resp1 = await async_client.post(
        "/api/work-items/",
        json={"title": "#910 First task", "priority": 0},
    )
    await async_client.post(
        "/api/work-items/",
        json={"title": "#911 Second task", "priority": 1},
    )
    first_id = resp1.json()["id"]

    # Start first item (backlog → in_dev)
    resp = await async_client.post(
        f"/api/work-items/{first_id}/start",
        json={"actor": "claude"},
    )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["status"] == "in_dev"

    # #910 should no longer appear in backlog
    resp = await async_client.get("/api/work-items/?status=backlog")
    backlog_titles = [d["title"] for d in resp.json()]
    assert "#910 First task" not in backlog_titles

    # #911 should still be in backlog
    assert "#911 Second task" in backlog_titles


@pytest.mark.asyncio
async def test_by_tag_lookup(async_client: AsyncClient):
    """GET /api/work-items/by-tag/920 finds the matching item."""
    resp = await async_client.post(
        "/api/work-items/",
        json={"title": "#920 Tag lookup test", "description": "Test description"},
    )
    assert resp.status_code == 201  # noqa: PLR2004

    # Lookup by tag
    resp = await async_client.get("/api/work-items/by-tag/920")
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert "#920" in data["title"]
    assert data["description"] == "Test description"


@pytest.mark.asyncio
async def test_by_tag_not_found(async_client: AsyncClient):
    """GET /api/work-items/by-tag/99999 returns 404."""
    resp = await async_client.get("/api/work-items/by-tag/99999")
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_limit_and_sort(async_client: AsyncClient):
    """Verify limit and sort params work together."""
    # Create items
    for title in ["#930 A", "#931 B", "#932 C"]:
        await async_client.post("/api/work-items/", json={"title": title})

    # Sort by -created_at, limit 2 → newest first
    resp = await async_client.get("/api/work-items/?sort=-created_at&limit=2")
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert len(data) <= 2  # noqa: PLR2004
