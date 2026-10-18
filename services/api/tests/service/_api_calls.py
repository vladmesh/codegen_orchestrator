"""Real API requests shared by the api service tests."""

from httpx import AsyncClient


async def call_api(client: AsyncClient, method: str, path: str, status: int = 200, **kwargs):
    """One API request answered with `status`, as its JSON body."""
    response = await client.request(method, path, **kwargs)
    assert response.status_code == status, response.text
    return response.json()
