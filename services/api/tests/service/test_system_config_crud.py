"""Service tests for system_configs CRUD API."""

from http import HTTPStatus

from httpx import AsyncClient
import pytest


@pytest.mark.asyncio
async def test_create_system_config(async_client: AsyncClient):
    resp = await async_client.post(
        "/api/system-configs/",
        json={
            "key": "test.create_key",
            "value": 42,
            "description": "Test config",
            "category": "test",
            "updated_by": "test",
        },
    )
    assert resp.status_code == HTTPStatus.CREATED
    data = resp.json()
    assert data["key"] == "test.create_key"
    assert data["value"] == 42
    assert data["category"] == "test"
    assert data["description"] == "Test config"


@pytest.mark.asyncio
async def test_upsert_system_config(async_client: AsyncClient):
    """POST with existing key updates instead of failing."""
    await async_client.post(
        "/api/system-configs/",
        json={"key": "test.upsert_key", "value": 1, "category": "test"},
    )
    resp = await async_client.post(
        "/api/system-configs/",
        json={"key": "test.upsert_key", "value": 99, "category": "test"},
    )
    assert resp.status_code == HTTPStatus.CREATED
    assert resp.json()["value"] == 99


@pytest.mark.asyncio
async def test_list_system_configs(async_client: AsyncClient):
    # Create two configs in same category
    for key in ["test.list_a", "test.list_b"]:
        await async_client.post(
            "/api/system-configs/",
            json={"key": key, "value": 1, "category": "list_test"},
        )

    resp = await async_client.get("/api/system-configs/", params={"category": "list_test"})
    assert resp.status_code == HTTPStatus.OK
    keys = [c["key"] for c in resp.json()]
    assert "test.list_a" in keys
    assert "test.list_b" in keys


@pytest.mark.asyncio
async def test_get_system_config(async_client: AsyncClient):
    await async_client.post(
        "/api/system-configs/",
        json={"key": "test.get_key", "value": "hello", "category": "test"},
    )
    resp = await async_client.get("/api/system-configs/test.get_key")
    assert resp.status_code == HTTPStatus.OK
    assert resp.json()["value"] == "hello"


@pytest.mark.asyncio
async def test_get_system_config_not_found(async_client: AsyncClient):
    resp = await async_client.get("/api/system-configs/nonexistent.key")
    assert resp.status_code == HTTPStatus.NOT_FOUND


@pytest.mark.asyncio
async def test_patch_system_config(async_client: AsyncClient):
    await async_client.post(
        "/api/system-configs/",
        json={"key": "test.patch_key", "value": 10, "category": "test"},
    )
    resp = await async_client.patch(
        "/api/system-configs/test.patch_key",
        json={"value": 20, "updated_by": "admin"},
    )
    assert resp.status_code == HTTPStatus.OK
    data = resp.json()
    assert data["value"] == 20
    assert data["updated_by"] == "admin"


@pytest.mark.asyncio
async def test_delete_system_config(async_client: AsyncClient):
    await async_client.post(
        "/api/system-configs/",
        json={"key": "test.delete_key", "value": 1, "category": "test"},
    )
    resp = await async_client.delete("/api/system-configs/test.delete_key")
    assert resp.status_code == HTTPStatus.NO_CONTENT

    resp = await async_client.get("/api/system-configs/test.delete_key")
    assert resp.status_code == HTTPStatus.NOT_FOUND


@pytest.mark.asyncio
async def test_system_config_json_complex_values(async_client: AsyncClient):
    """Value supports dicts, lists, nested structures."""
    await async_client.post(
        "/api/system-configs/",
        json={"key": "test.complex", "value": {"nested": [1, 2]}, "category": "test"},
    )
    resp = await async_client.get("/api/system-configs/test.complex")
    assert resp.json()["value"] == {"nested": [1, 2]}
