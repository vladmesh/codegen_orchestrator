"""Base utilities for database tools.

Provides InternalAPIClient singleton for consistent API access.
"""

import os
from typing import Any

import httpx


class InternalAPIClient:
    """Singleton async HTTP client for internal API.
    
    Reduces boilerplate and provides consistent error handling
    across all database tools.
    """
    
    def __init__(self):
        self.base_url = os.getenv("API_URL", "http://api:8000")
        self._client: httpx.AsyncClient | None = None
    
    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create async client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                follow_redirects=True,
                timeout=30.0,
            )
        return self._client
    
    async def get(self, path: str, **kwargs) -> dict | list:
        """Make GET request to internal API."""
        client = await self._get_client()
        resp = await client.get(path, **kwargs)
        resp.raise_for_status()
        return resp.json()
    
    async def post(self, path: str, **kwargs) -> dict:
        """Make POST request to internal API."""
        client = await self._get_client()
        resp = await client.post(path, **kwargs)
        resp.raise_for_status()
        return resp.json()
    
    async def patch(self, path: str, **kwargs) -> dict:
        """Make PATCH request to internal API."""
        client = await self._get_client()
        resp = await client.patch(path, **kwargs)
        resp.raise_for_status()
        return resp.json()
    
    async def delete(self, path: str, **kwargs) -> dict | None:
        """Make DELETE request to internal API."""
        client = await self._get_client()
        resp = await client.delete(path, **kwargs)
        resp.raise_for_status()
        if resp.status_code == 204:
            return None
        return resp.json()
    
    async def get_raw(self, path: str, **kwargs) -> httpx.Response:
        """Make GET request and return raw response (for status code checks)."""
        client = await self._get_client()
        return await client.get(path, **kwargs)
    
    async def close(self):
        """Close the client connection."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# Singleton instance
api_client = InternalAPIClient()

# Legacy constant for backward compatibility
INTERNAL_API_URL = os.getenv("API_URL", "http://api:8000")
