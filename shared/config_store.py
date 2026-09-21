"""ConfigStore — read-only client for system_configs with TTL cache.

Reads operational constants from the API. No business logic — just
HTTP GET + in-memory caching with TTL.

It is read at service startup, from synchronous code, so it takes the
synchronous form of the shared transport rather than raw `httpx`: these reads are
internal API calls and carry the same two headers as every other one.

Callers must opt a key into bounded last-known-good reads explicitly. Keys with
no stale policy fail closed when the source cannot be read.
"""

from collections.abc import Mapping
from dataclasses import dataclass
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


@dataclass(frozen=True)
class BoundedStalePolicy:
    """Allow a cached config value only up to a bounded age during source failure."""

    max_age_seconds: float

    def __post_init__(self) -> None:
        if self.max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be greater than zero")


@dataclass(frozen=True)
class _CachedConfig:
    value: Any
    expires_at: float
    fetched_at: float


class ConfigStore:
    """Read system configs from API with an in-memory TTL cache.

    Normal cache hits use `cache_ttl`. Once a refresh is due and the source is
    unavailable, keys fail closed unless the caller supplied a BoundedStalePolicy
    for that exact key.
    """

    def __init__(
        self,
        api_base_url: str,
        cache_ttl: int = 30,
        *,
        stale_policies: Mapping[str, BoundedStalePolicy] | None = None,
    ):
        self._client = InternalAPISyncClient(api_base_url, timeout=10.0)
        self._cache_ttl = cache_ttl
        self._stale_policies = dict(stale_policies or {})
        self._cache: dict[str, _CachedConfig] = {}
        self._lock = threading.Lock()

    def _source_unavailable(self, key: str, reason: str, cause: Exception | None) -> Any:
        """Use a bounded last-known value for explicitly opted-in keys, or fail closed."""
        with self._lock:
            cached = self._cache.get(key)

        policy = self._stale_policies.get(key)
        if cached is not None and policy is not None:
            stale_age_seconds = max(time.monotonic() - cached.fetched_at, 0.0)
            if stale_age_seconds <= policy.max_age_seconds:
                logger.warning(
                    "config_store_source_unavailable_using_bounded_stale",
                    key=key,
                    reason=reason,
                    stale_age_seconds=round(stale_age_seconds, 3),
                    max_stale_age_seconds=policy.max_age_seconds,
                )
                return cached.value

            logger.error(
                "config_store_source_unavailable_stale_expired",
                key=key,
                reason=reason,
                stale_age_seconds=round(stale_age_seconds, 3),
                max_stale_age_seconds=policy.max_age_seconds,
            )

        raise ConfigStoreUnavailableError(
            f"System config API is unavailable while reading '{key}' ({reason})"
        ) from cause

    def get(self, key: str, default: Any = _DEFAULT_SENTINEL) -> Any:
        """Get a config value by key. Raises KeyError if not found and no default."""
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached.expires_at > time.monotonic():
                return cached.value

        try:
            resp = self._client.get_raw(f"system-configs/{key}")
        except httpx.RequestError as exc:
            return self._source_unavailable(key, f"request failed: {exc}", exc)

        if resp.status_code == httpx.codes.OK:
            try:
                value = resp.json()["value"]
            except (KeyError, TypeError, ValueError) as exc:
                return self._source_unavailable(key, "invalid response body", exc)
            now = time.monotonic()
            with self._lock:
                self._cache[key] = _CachedConfig(
                    value=value,
                    expires_at=now + self._cache_ttl,
                    fetched_at=now,
                )
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
