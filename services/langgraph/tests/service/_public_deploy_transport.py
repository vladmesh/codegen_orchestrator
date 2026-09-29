"""Authenticated App content reads; all requests end in deterministic HTTP."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt

import shared
from shared.clients.github import GitHubAppClient


def released_workflow():
    from pathlib import Path

    fixtures = Path(shared.__file__).resolve().parent / "tests/fixtures"
    paths = list(fixtures.glob("*/.github/workflows/deploy.yml"))
    assert len(paths) == 1
    return paths[0].read_text()


class ContentGitHubFixture(GitHubAppClient):
    def __init__(self, built, source, rejected=None):
        super().__init__()
        self.app_id = "1439"
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key = key.public_key()
        self._private_key = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        self.built, self.source, self.rejected = built, source, rejected
        self.reads = []
        self._http_client = httpx.AsyncClient(transport=httpx.MockTransport(self.respond))
        # External effects remain nonconnecting, after actual content admission.
        for name in (
            "set_repository_secrets",
            "create_or_reset_tag",
            "delete_ref",
            "trigger_workflow_dispatch",
            "rerun_failed_jobs",
            "fence_workflow",
        ):
            setattr(self, name, AsyncMock())
        self.ref_exists = AsyncMock(return_value=False)

    def respond(self, request):
        assert request.url.host == "api.github.com"
        path = request.url.path
        if path in {"/repos/fixture/public/installation", "/app/installations/1439/access_tokens"}:
            jwt.decode(
                request.headers["Authorization"].removeprefix("Bearer "),
                self.public_key,
                algorithms=["RS256"],
                issuer=self.app_id,
            )
            if path.endswith("installation"):
                assert request.method == "GET"
                return httpx.Response(200, json={"id": 1439})
            assert request.method == "POST"
            return httpx.Response(
                201,
                json={
                    "token": "content-fixture-token",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                },
            )
        assert request.method == "GET"
        assert request.headers["Authorization"] == "token content-fixture-token"
        assert request.headers["Accept"] == "application/vnd.github.raw+json"
        assert dict(request.url.params) == {"ref": self.built}
        assert path in {
            "/repos/fixture/public/contents/.github/workflows/deploy.yml",
            "/repos/fixture/public/contents/.github/workflows/deploy.yml.rej",
        }
        self.reads.append((path, self.built))
        source = self.rejected if path.endswith(".rej") else self.source
        if source is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if isinstance(source, Exception):
            return httpx.Response(401, text=str(source))
        return httpx.Response(200, text=source)

    async def close(self):
        await self._http_client.aclose()
