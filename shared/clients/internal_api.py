"""Shared transport for authenticated internal API requests."""

from __future__ import annotations

import os

import httpx

from shared.log_config.correlation import ensure_correlation_id, set_correlation_id

DEFAULT_TIMEOUT_SECONDS = 30.0

INTERNAL_KEY_HEADER = "X-Internal-Key"
CORRELATION_ID_HEADER = "X-Correlation-ID"

# HTTP header names are case-insensitive, so `x-internal-key` from a caller is
# the same field as the one the transport sets. Both spellings on one request
# means two fields with one name on the wire, and the API reads the first.
_MANDATORY_HEADERS_LOWERCASED = frozenset(
    {INTERNAL_KEY_HEADER.lower(), CORRELATION_ID_HEADER.lower()}
)


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
        """Both headers, once each, in their canonical spelling.

        A caller cannot drop `X-Internal-Key` and cannot shadow it with another
        spelling: whatever case it used, the field is taken out of its headers
        before the transport sets its own, so the request carries exactly one of
        each name. Reading the caller's names case-insensitively is why a
        `x-internal-key: forged` no longer arrives ahead of the real key.

        An unbound correlation context no longer means an unlabelled call. The
        identifier of the flow is decided first — the one the caller named, in
        any spelling, otherwise the bound one, otherwise a fresh one — and only
        then is it both bound and sent, so what goes on the wire and what the
        rest of the flow will carry are never two different identifiers.
        """
        headers = {}
        named = None
        for name, value in (caller_headers or {}).items():
            lowered = name.lower()
            if lowered not in _MANDATORY_HEADERS_LOWERCASED:
                headers[name] = value
                continue
            if lowered == CORRELATION_ID_HEADER.lower() and named is None and value:
                named = value

        headers[INTERNAL_KEY_HEADER] = self._internal_api_key
        if named:
            set_correlation_id(named)
            headers[CORRELATION_ID_HEADER] = named
        else:
            headers[CORRELATION_ID_HEADER] = ensure_correlation_id()
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

    async def delete_raw(self, path: str, **kwargs) -> httpx.Response:
        return await self.request_raw("DELETE", path, **kwargs)

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
