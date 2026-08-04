"""ConfigStore — read-only client for system_configs with TTL cache.

Reads operational constants from the API. No business logic — just
HTTP GET + in-memory caching with TTL.

It is read at service startup, from synchronous code, so it takes the
synchronous form of the shared transport rather than raw `httpx`: these reads are
internal API calls and carry the same two headers as every other one.

Usage:
    store = ConfigStore(api_base_url="http://api:8000")
    interval = store.get_int("scheduler.dispatch_interval_seconds")
    thresholds = store.get_category("health")
"""

import threading
import time
from typing import Any

import httpx
import structlog

from shared.clients.internal_api import InternalAPISyncClient

logger = structlog.get_logger()

_DEFAULT_SENTINEL = object()


class ConfigStoreUnavailableError(RuntimeError):
    """Raised when the system-config API cannot answer a config request."""


class ConfigStore:
    """Read system configs from API with in-memory TTL cache."""

    def __init__(self, api_base_url: str, cache_ttl: int = 30):
        self._client = InternalAPISyncClient(api_base_url, timeout=10.0)
        self._cache_ttl = cache_ttl
        self._cache: dict[str, tuple[Any, float]] = {}  # key -> (value, expires_at)
        self._lock = threading.Lock()

    def _source_unavailable(self, key: str, reason: str, cause: Exception | None) -> Any:
        """Return the last known value for `key`, or raise if there is none.

        An unreachable or broken config source is not the same as a missing key:
        callers already running on a value keep running on it, and only a caller
        that never read the key at all gets an error.
        """
        with self._lock:
            cached = self._cache.get(key)

        if cached is not None:
            logger.warning(
                "config_store_source_unavailable_using_last_known",
                key=key,
                reason=reason,
                value=cached[0],
            )
            return cached[0]

        raise ConfigStoreUnavailableError(
            f"System config API is unavailable while reading '{key}' ({reason})"
        ) from cause

    def get(self, key: str, default: Any = _DEFAULT_SENTINEL) -> Any:
        """Get a config value by key. Raises KeyError if not found and no default."""
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[1] > time.monotonic():
                return cached[0]

        try:
            resp = self._client.get_raw(f"system-configs/{key}")
        except httpx.RequestError as exc:
            return self._source_unavailable(key, f"request failed: {exc}", exc)

        if resp.status_code == httpx.codes.OK:
            try:
                value = resp.json()["value"]
            except (KeyError, TypeError, ValueError) as exc:
                return self._source_unavailable(key, "invalid response body", exc)
            with self._lock:
                self._cache[key] = (value, time.monotonic() + self._cache_ttl)
            return value
        if resp.status_code != httpx.codes.NOT_FOUND:
            return self._source_unavailable(key, f"HTTP {resp.status_code}", None)

        if default is not _DEFAULT_SENTINEL:
            return default
        raise KeyError(f"System config '{key}' not found")

    def get_int(self, key: str, default: int | None = None) -> int:
        """Get config value as int."""
        sentinel = _DEFAULT_SENTINEL if default is None else default
        value = self.get(key, sentinel)
        return int(value)

    def get_float(self, key: str, default: float | None = None) -> float:
        """Get config value as float."""
        sentinel = _DEFAULT_SENTINEL if default is None else default
        value = self.get(key, sentinel)
        return float(value)

    def get_category(self, category: str) -> dict[str, Any]:
        """Get all configs in a category as {key: value} dict."""
        try:
            resp = self._client.get_raw("system-configs/", params={"category": category})
            if resp.status_code == httpx.codes.OK:
                result = {}
                for item in resp.json():
                    key = item["key"]
                    value = item["value"]
                    result[key] = value
                    with self._lock:
                        self._cache[key] = (value, time.monotonic() + self._cache_ttl)
                return result
        except httpx.RequestError:
            logger.warning("config_store_category_fetch_failed", category=category)

        return {}

    def validate_required(self, keys: list[str]) -> None:
        """Validate that all required config keys exist in the DB.

        Raises RuntimeError listing all missing keys — call at service startup.
        A key that is declared in scripts/system_configs.yaml is missing only if
        the seeding step of the deploy did not run.
        """
        missing = []
        for key in keys:
            try:
                self.get(key)
            except KeyError:
                missing.append(key)

        if missing:
            raise RuntimeError(
                f"Missing required system configs: {', '.join(missing)}. "
                f"Run `make seed` to populate defaults."
            )
