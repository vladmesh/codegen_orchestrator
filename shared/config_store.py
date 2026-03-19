"""ConfigStore — read-only client for system_configs with TTL cache.

Reads operational constants from the API. No business logic — just
HTTP GET + in-memory caching with TTL.

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

logger = structlog.get_logger()

_DEFAULT_SENTINEL = object()


class ConfigStore:
    """Read system configs from API with in-memory TTL cache."""

    def __init__(self, api_base_url: str, cache_ttl: int = 30):
        self._api_base_url = api_base_url.rstrip("/")
        self._cache_ttl = cache_ttl
        self._cache: dict[str, tuple[Any, float]] = {}  # key -> (value, expires_at)
        self._lock = threading.Lock()

    def _api_url(self, path: str) -> str:
        return f"{self._api_base_url}/api/{path.lstrip('/')}"

    def get(self, key: str, default: Any = _DEFAULT_SENTINEL) -> Any:
        """Get a config value by key. Raises KeyError if not found and no default."""
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[1] > time.monotonic():
                return cached[0]

        try:
            resp = httpx.get(self._api_url(f"system-configs/{key}"), timeout=10.0)
            if resp.status_code == httpx.codes.OK:
                value = resp.json()["value"]
                with self._lock:
                    self._cache[key] = (value, time.monotonic() + self._cache_ttl)
                return value
        except httpx.RequestError:
            # If API is down and we have a stale cache entry, use it
            with self._lock:
                cached = self._cache.get(key)
                if cached:
                    logger.warning("config_store_using_stale_cache", key=key)
                    return cached[0]

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
            resp = httpx.get(
                self._api_url("system-configs/"),
                params={"category": category},
                timeout=10.0,
            )
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
