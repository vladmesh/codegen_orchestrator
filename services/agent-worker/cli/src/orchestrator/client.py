import os
from typing import Any

import httpx


class APIClient:
    def __init__(self):
        self.base_url = os.getenv("ORCHESTRATOR_API_URL", "http://api:8000")
        self.user_id = os.getenv("ORCHESTRATOR_USER_ID")
        self.api_token = os.getenv("ORCHESTRATOR_API_TOKEN")  # Future use

        if not self.user_id:
            # In dev/test when running outside container, fallback might be needed or strict error
            # For now, let's assume it's required as per spec
            pass

        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"X-User-ID": self.user_id or "anonymous", "Content-Type": "application/json"},
            timeout=30.0,
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def post(self, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.client.post(path, json=json)
        response.raise_for_status()
        return response.json()

    def put(self, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.client.put(path, json=json)
        response.raise_for_status()
        return response.json()

    def delete(self, path: str) -> dict[str, Any]:
        response = self.client.delete(path)
        response.raise_for_status()
        return response.json()
