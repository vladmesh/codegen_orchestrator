"""The process's one system-config snapshot and the only place that refreshes it.

`start_system_config` is awaited once at process startup; readers call
`system_config()` and get in-memory lookups with the ConfigStore rules.
"""

from __future__ import annotations

import asyncio

import structlog

from shared.config_store import ConfigStore, ConfigStoreUnavailableError

logger = structlog.get_logger(__name__)

_store: ConfigStore | None = None
_refresher: asyncio.Task[None] | None = None


def system_config() -> ConfigStore:
    """Return the store `start_system_config` built for this process."""
    if _store is None:
        raise RuntimeError("System config is not started; await start_system_config() first")
    return _store


async def start_system_config(api_base_url: str, *, cache_ttl: float) -> ConfigStore:
    """Build the process store, await its first refresh, and keep refreshing it in the background.

    A failed first refresh is logged and leaves the store without a snapshot, so
    its reads fail closed until a background refresh succeeds.
    """
    global _store, _refresher  # noqa: PLW0603 - one store per process
    store = ConfigStore(api_base_url, cache_ttl=cache_ttl)
    try:
        await store.refresh()
    except ConfigStoreUnavailableError as exc:
        logger.warning("system_config_first_refresh_failed", error=str(exc))
    _store = store
    _refresher = asyncio.create_task(store.refresh_forever(), name="system_config_refresh")
    _refresher.add_done_callback(_refresher_stopped)
    return store


async def stop_system_config() -> None:
    """Stop the refresher and release the store, if started; reads raise until the next start."""
    global _store, _refresher  # noqa: PLW0603 - one store per process
    if _refresher is not None:
        _refresher.cancel()
        await asyncio.gather(_refresher, return_exceptions=True)
    if _store is not None:
        await _store.close()
    _store = None
    _refresher = None


def _refresher_stopped(task: asyncio.Task[None]) -> None:
    # Reads then fail closed once the snapshot outlives its TTL; this names the cause.
    if not task.cancelled() and task.exception() is not None:
        logger.error("system_config_refresher_stopped", exc_info=task.exception())
