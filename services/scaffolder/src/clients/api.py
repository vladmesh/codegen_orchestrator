"""Thin HTTP client for scaffolder API calls."""

from __future__ import annotations

import httpx
import structlog

from shared.log_config.correlation import get_correlation_id
from src.config import get_settings

logger = structlog.get_logger(__name__)

_client: ScaffolderAPIClient | None = None


def get_api_client() -> ScaffolderAPIClient:
    global _client
    if _client is None:
        _client = ScaffolderAPIClient()
    return _client


class ScaffolderAPIClient:
    """HTTP client for project/repository updates."""

    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = settings.api_base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                follow_redirects=True,
                timeout=30.0,
            )
        return self._client

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        client = await self._get_client()
        correlation_id = get_correlation_id()
        if correlation_id:
            headers = kwargs.pop("headers", None) or {}
            headers.setdefault("X-Correlation-ID", correlation_id)
            kwargs["headers"] = headers
        resp = await client.request(method, f"/api/{path.lstrip('/')}", **kwargs)
        resp.raise_for_status()
        return resp

    async def get_project(self, project_id: str) -> dict:
        resp = await self._request("GET", f"projects/{project_id}")
        return resp.json()

    async def get_repository(self, repo_id: str) -> dict:
        resp = await self._request("GET", f"repositories/{repo_id}")
        return resp.json()

    async def update_project_status(self, project_id: str, status: str) -> None:
        await self._request(
            "PATCH",
            f"projects/{project_id}",
            json={"status": status},
        )
        logger.info("project_status_updated", project_id=project_id, status=status)

    async def update_repository(self, repo_id: str, **fields) -> None:
        await self._request("PATCH", f"repositories/{repo_id}", json=fields)
        logger.info("repository_updated", repo_id=repo_id, fields=list(fields.keys()))

    async def update_project_config(self, project_id: str, config: dict) -> None:
        await self._request(
            "PATCH",
            f"projects/{project_id}",
            json={"config": config},
        )
        logger.info("project_config_updated", project_id=project_id)

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
