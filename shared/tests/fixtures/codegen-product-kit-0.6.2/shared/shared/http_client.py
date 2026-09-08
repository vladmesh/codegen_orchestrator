"""Base HTTP client with retry and exponential backoff."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
import os
from typing import Any, Self

import httpx
import structlog

LOGGER = structlog.stdlib.get_logger()


class ServiceClient:
    """Async HTTP client with retry for inter-service communication."""

    def __init__(
        self,
        base_url: str | None = None,
        base_url_env: str = "",
        timeout: float = 10.0,
        max_retries: int = 3,
        initial_delay: float = 1.0,
    ) -> None:
        self.base_url = base_url or os.getenv(base_url_env) or ""
        if not self.base_url:
            raise ValueError(
                f"{type(self).__name__} requires base_url or {base_url_env} environment variable"
            )
        self.timeout = timeout
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                f"{type(self).__name__} must be used as async context manager: "
                f"async with {type(self).__name__}() as client: ..."
            )
        return self._client

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """HTTP request with retry on 5xx/ConnectError, immediate fail on 4xx."""
        client = self._ensure_client()
        delay = self.initial_delay
        last_exception: Exception | None = None

        for attempt in range(self.max_retries):
            try:
                response = await getattr(client, method)(path, **kwargs)
                response.raise_for_status()
                return response

            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if HTTPStatus.BAD_REQUEST <= status < HTTPStatus.INTERNAL_SERVER_ERROR:
                    raise
                last_exception = e
                if attempt < self.max_retries - 1:
                    LOGGER.warning(
                        "%s returned %s (attempt %d/%d), retrying in %.1fs...",
                        self.base_url,
                        status,
                        attempt + 1,
                        self.max_retries,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    delay *= 2

            except httpx.ConnectError as e:
                last_exception = e
                if attempt < self.max_retries - 1:
                    LOGGER.warning(
                        "%s unavailable (attempt %d/%d), retrying in %.1fs...",
                        self.base_url,
                        attempt + 1,
                        self.max_retries,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    delay *= 2

        LOGGER.error(
            "Request to %s failed after %d attempts",
            self.base_url,
            self.max_retries,
        )
        if last_exception:
            raise last_exception
        raise RuntimeError("Unexpected retry loop exit")
