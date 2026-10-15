"""ConfigStore: an in-memory snapshot of every system config, refreshed off the read path.

`refresh()` replaces the snapshot from one batch read through the async internal
API client, so a read never does I/O and carries the same two headers as every
other internal call.

Callers must opt a key into bounded last-known-good reads explicitly. Keys with
no stale policy fail closed when the snapshot cannot be kept fresh.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import time
from typing import Any

import httpx
import structlog

from shared.clients.internal_api import InternalAPIClient

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
class _Snapshot:
    values: Mapping[str, Any]
    fetched_at: float


@dataclass(frozen=True)
class _RefreshFailure:
    reason: str
    cause: Exception | None


class ConfigStore:
    """Every system config as one snapshot that is fresh for `cache_ttl` seconds.

    Before the first refresh, or once the snapshot outlives `cache_ttl`, keys fail
    closed unless the caller supplied a BoundedStalePolicy for that exact key.
    """

    def __init__(
        self,
        api_base_url: str,
        cache_ttl: float = 30,
        *,
        stale_policies: Mapping[str, BoundedStalePolicy] | None = None,
    ):
        if cache_ttl <= 0:
            raise ValueError("cache_ttl must be greater than zero")
        self._client = InternalAPIClient(api_base_url, timeout=10.0)
        self._cache_ttl = cache_ttl
        self._stale_policies = dict(stale_policies or {})
        self._snapshot: _Snapshot | None = None
        self._failure: _RefreshFailure | None = None

    async def refresh(self) -> None:
        """Replace the snapshot from one read of all keys.

        A failed read keeps the previous snapshot, records why for the reads that
        find it stale, and raises ConfigStoreUnavailableError.
        """
        try:
            resp = await self._client.get_raw("system-configs/")
        except httpx.RequestError as exc:
            raise self._record_failure(f"request failed: {exc}", exc) from exc
        if resp.status_code != httpx.codes.OK:
            raise self._record_failure(f"HTTP {resp.status_code}", None)
        try:
            body = resp.json()
            if not isinstance(body, list):
                raise TypeError(f"expected a list, got {type(body).__name__}")
            values = {entry["key"]: entry["value"] for entry in body}
        except (KeyError, TypeError, ValueError) as exc:
            raise self._record_failure("invalid response body", exc) from exc
        self._snapshot = _Snapshot(values=values, fetched_at=time.monotonic())
        self._failure = None

    async def refresh_forever(
        self, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    ) -> None:
        """Refresh every half TTL until cancelled; a failed refresh waits for the next one."""
        # Half the TTL keeps a healthy snapshot fresh while one read takes its time.
        while True:
            await sleep(self._cache_ttl / 2)
            try:
                await self.refresh()
            except ConfigStoreUnavailableError as exc:
                logger.warning("config_store_refresh_failed", error=str(exc))

    async def close(self) -> None:
        await self._client.close()

    def _record_failure(self, reason: str, cause: Exception | None) -> ConfigStoreUnavailableError:
        self._failure = _RefreshFailure(reason=reason, cause=cause)
        return ConfigStoreUnavailableError(f"System config API is unavailable ({reason})")

    def _source_unavailable(self, key: str, snapshot: _Snapshot | None) -> Any:
        """Use a bounded last-known value for explicitly opted-in keys, or fail closed."""
        failure = self._failure
        if failure is not None:
            reason = failure.reason
        elif snapshot is None:
            reason = "no snapshot yet"
        else:
            reason = f"no refresh within {self._cache_ttl}s"

        policy = self._stale_policies.get(key)
        if snapshot is not None and policy is not None and key in snapshot.values:
            stale_age_seconds = max(time.monotonic() - snapshot.fetched_at, 0.0)
            if stale_age_seconds <= policy.max_age_seconds:
                logger.warning(
                    "config_store_source_unavailable_using_bounded_stale",
                    key=key,
                    reason=reason,
                    stale_age_seconds=round(stale_age_seconds, 3),
                    max_stale_age_seconds=policy.max_age_seconds,
                )
                return snapshot.values[key]

            logger.error(
                "config_store_source_unavailable_stale_expired",
                key=key,
                reason=reason,
                stale_age_seconds=round(stale_age_seconds, 3),
                max_stale_age_seconds=policy.max_age_seconds,
            )

        raise ConfigStoreUnavailableError(
            f"System config API is unavailable while reading '{key}' ({reason})"
        ) from (failure.cause if failure is not None else None)

    def get(self, key: str, default: Any = _DEFAULT_SENTINEL) -> Any:
        """Get a config value from the snapshot. Raises KeyError if not found and no default."""
        snapshot = self._snapshot
        if snapshot is None or time.monotonic() - snapshot.fetched_at >= self._cache_ttl:
            return self._source_unavailable(key, snapshot)
        if key in snapshot.values:
            return snapshot.values[key]
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
        """Validate that all required config keys exist in the snapshot.

        Raises RuntimeError listing all missing keys; call at service startup,
        after the first refresh. A key that is declared in scripts/system_configs.yaml
        is missing only if the seeding step of the deploy did not run.
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
