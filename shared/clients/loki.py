"""Loki HTTP client — LogQL range queries over Loki's HTTP API."""

from datetime import datetime
import os

import httpx
import structlog

logger = structlog.get_logger()


def _get_env(key: str) -> str:
    """Read env var, raising RuntimeError if missing."""
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"{key} is not set")
    return val


class LokiClient:
    """Async client for Loki's query_range HTTP API."""

    def __init__(
        self,
        base_url: str | None = None,
        user: str | None = None,
        password: str | None = None,
    ):
        if base_url is None:
            base_url = _get_env("LOKI_URL")
        if user is None:
            user = os.environ.get("LOKI_USER", "")
        if password is None:
            password = os.environ.get("LOKI_PASSWORD", "")
        auth = (user, password) if user else None
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            auth=auth,
            timeout=30.0,
        )

    async def query_range(
        self,
        query: str,
        start: datetime,
        end: datetime,
        limit: int = 5000,
    ) -> list[dict]:
        """Execute a LogQL range query and return parsed log entries.

        Returns a flat list of parsed JSON log lines from all streams.
        """
        params = {
            "query": query,
            "start": str(int(start.timestamp())),
            "end": str(int(end.timestamp())),
            "limit": str(limit),
            "direction": "forward",
        }

        logger.debug(
            "loki_query_range",
            query=query,
            start=start.isoformat(),
            end=end.isoformat(),
        )

        resp = await self._client.get("/loki/api/v1/query_range", params=params)
        resp.raise_for_status()

        data = resp.json()
        return self._parse_response(data)

    @staticmethod
    def _parse_response(data: dict) -> list[dict]:
        """Parse Loki query_range JSON response into flat list of log entries.

        Loki response format:
        {
          "data": {
            "result": [
              {
                "stream": {"label": "value"},
                "values": [
                  ["<unix_nano_ts>", "<log_line_json>"]
                ]
              }
            ]
          }
        }
        """
        import json

        entries: list[dict] = []
        for stream in data["data"]["result"]:
            labels = stream["stream"]
            for _ts, line in stream["values"]:
                try:
                    parsed = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    parsed = {"raw": line}
                parsed["_labels"] = labels
                entries.append(parsed)
        return entries

    async def close(self):
        """Close the underlying HTTP client."""
        await self._client.aclose()
