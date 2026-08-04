"""The transport every service uses to reach the internal API.

Five services used to carry a near-identical copy of `_get_client` / `_api_path` /
`_request`, and a copy could quietly disagree: `telegram_bot` sent no
`X-Internal-Key` at all. The wire contract lives here now — the `/api` prefix, the
two headers, `raise_for_status()` — so a caller cannot forget a header without
rewriting this module. Services keep their own application methods and subclass
this for the transport; the only thing they vary is the timeout.

Two callers in `shared/` are synchronous or read the internal API outside a
service client (`config_store`, `notifications`), so the wire contract is written
once in `InternalAPITransport` and the async and sync clients only differ in how
they send.

`shared` is a tree, not a package (docs/decisions/shared-is-not-a-package.md), so
this is a plain module next to the other clients.
"""

from __future__ import annotations

import os

import httpx

from shared.log_config.correlation import ensure_correlation_id

DEFAULT_TIMEOUT_SECONDS = 30.0


class InternalAPITransport:
    """URL shape and headers of the internal API. Subclasses do the sending."""

    def __init__(self, base_url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith("/api"):
            raise RuntimeError("API_BASE_URL must not include /api")
        self._internal_api_key = os.environ["INTERNAL_API_KEY"]
        self._timeout = timeout

    def api_path(self, path: str) -> str:
        cleaned = path.lstrip("/")
        if cleaned.startswith("api/"):
            raise ValueError("API path should not include /api prefix")
        return f"/api/{cleaned}"

    def request_headers(self, caller_headers: dict | None) -> dict:
        """Both headers, on every request.

        A caller cannot drop `X-Internal-Key`, and an unbound correlation context
        no longer means an unlabelled call: the transport creates the identifier
        and binds it for the rest of the flow.
        """
        headers = dict(caller_headers or {})
        headers["X-Internal-Key"] = self._internal_api_key
        headers.setdefault("X-Correlation-ID", ensure_correlation_id())
        return headers


class InternalAPIClient(InternalAPITransport):
    """Lazy httpx client for the internal API."""

    def __init__(self, base_url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        super().__init__(base_url, timeout=timeout)
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                follow_redirects=True,
                timeout=self._timeout,
            )
        return self._client

    async def request_raw(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Send a request and hand the response back whatever its status.

        For callers that read a status code themselves — a 422 the API returns as
        a user-facing verdict, a 404 that means "not there yet".
        """
        client = await self._get_client()
        headers = self.request_headers(kwargs.pop("headers", None))
        return await client.request(method, self.api_path(path), headers=headers, **kwargs)

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        resp = await self.request_raw(method, path, **kwargs)
        resp.raise_for_status()
        return resp

    async def get_raw(self, path: str, **kwargs) -> httpx.Response:
        return await self.request_raw("GET", path, **kwargs)

    async def post_raw(self, path: str, **kwargs) -> httpx.Response:
        return await self.request_raw("POST", path, **kwargs)

    async def patch_raw(self, path: str, **kwargs) -> httpx.Response:
        return await self.request_raw("PATCH", path, **kwargs)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class InternalAPISyncClient(InternalAPITransport):
    """The same wire contract for callers that run outside an event loop."""

    def __init__(self, base_url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        super().__init__(base_url, timeout=timeout)
        self._client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                base_url=self.base_url,
                follow_redirects=True,
                timeout=self._timeout,
            )
        return self._client

    def request_raw(self, method: str, path: str, **kwargs) -> httpx.Response:
        client = self._get_client()
        headers = self.request_headers(kwargs.pop("headers", None))
        return client.request(method, self.api_path(path), headers=headers, **kwargs)

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        resp = self.request_raw(method, path, **kwargs)
        resp.raise_for_status()
        return resp

    def get_raw(self, path: str, **kwargs) -> httpx.Response:
        return self.request_raw("GET", path, **kwargs)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
