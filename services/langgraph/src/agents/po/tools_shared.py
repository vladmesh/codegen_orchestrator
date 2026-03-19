"""PO tools — shared clients and helpers.

Module-level httpx/Redis clients initialized at consumer startup via init_po_clients().
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.runnables import RunnableConfig

from shared.redis_client import RedisStreamClient

if TYPE_CHECKING:
    import httpx

# Module-level clients — set by init_po_clients()
_api_client: httpx.AsyncClient | None = None
_stream_client: RedisStreamClient | None = None


def init_po_clients(api_client: httpx.AsyncClient, stream_client: RedisStreamClient) -> None:
    """Initialize shared clients for PO tools. Called once at consumer startup."""
    global _api_client, _stream_client
    _api_client = api_client
    _stream_client = stream_client


def _get_api() -> httpx.AsyncClient:
    if _api_client is None:
        raise RuntimeError("PO tools not initialized — call init_po_clients() first")
    return _api_client


def _get_stream_client() -> RedisStreamClient:
    if _stream_client is None:
        raise RuntimeError("PO tools not initialized — call init_po_clients() first")
    return _stream_client


def _user_headers(config: RunnableConfig) -> dict[str, str]:
    """Extract X-Telegram-ID header from LangGraph config."""
    user_id = config["configurable"].get("user_id", "")
    if user_id:
        return {"X-Telegram-ID": str(user_id)}
    return {}
