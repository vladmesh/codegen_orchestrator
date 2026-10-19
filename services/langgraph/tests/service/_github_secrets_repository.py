"""A product repository's Actions secrets API over controlled HTTP, for real-Redis receipt tests."""

import base64
from datetime import UTC, datetime, timedelta
import json
import os
import random
from unittest.mock import AsyncMock, patch

import httpx
from nacl import public

from shared.clients.github import GitHubAppClient

OWNER, REPO = "fixture", "registry-receipt"


class Repository:
    """A GitHub repository with a fresh id; each client of it is a separate job's."""

    def __init__(self) -> None:
        self.id = random.randrange(10**9, 10**10)  # noqa: S311
        self._key = base64.b64encode(bytes(public.PrivateKey.generate().public_key)).decode()
        self.created: list[str] = []
        self.puts: list[str] = []
        self.key_reads = 0
        self.on_put = None
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def _description(self) -> dict:
        return {
            "id": self.id,
            "name": REPO,
            "full_name": f"{OWNER}/{REPO}",
            "html_url": f"https://github.com/{OWNER}/{REPO}",
            "clone_url": f"https://github.com/{OWNER}/{REPO}.git",
        }

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if (request.method, request.url.path) == ("POST", f"/orgs/{OWNER}/repos"):
            self.created.append(json.loads(request.content)["name"])
            return httpx.Response(201, json=self._description())
        path = request.url.path.removeprefix(f"/repos/{OWNER}/{REPO}")
        if path == "":
            return httpx.Response(200, json=self._description())
        if path == "/actions/secrets/public-key":
            self.key_reads += 1
            return httpx.Response(200, json={"key": self._key, "key_id": "k1"})
        self.puts.append(path.rsplit("/", 1)[1])
        if self.on_put is not None:
            await self.on_put()
        return httpx.Response(204)

    def client(self) -> GitHubAppClient:
        with patch.dict(os.environ, {"GITHUB_APP_ID": "12345"}):
            github = GitHubAppClient()
        github._http_client = self.http
        github._token_cache[111] = ("token", datetime.now(UTC) + timedelta(hours=1))
        github.get_installation_id = AsyncMock(return_value=111)
        github.get_org_installation_id = AsyncMock(return_value=111)
        return github
