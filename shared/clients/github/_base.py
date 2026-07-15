import asyncio
from datetime import UTC, datetime, timedelta
import os
import time
from typing import Any

import httpx
import jwt

from shared.log_config import get_logger

logger = get_logger(__name__)


class WorkflowNotFoundError(RuntimeError):
    """Raised when a GitHub Actions workflow file does not exist in the repository."""


class WorkflowCancelledError(RuntimeError):
    """Raised when a caller cancels an in-progress GitHub Actions workflow."""


class WorkflowCancellationUnprovenError(RuntimeError):
    """Raised when teardown cannot prove a GitHub Actions run has stopped."""


class GitHubAppClientBase:
    """Core GitHub App authentication and HTTP request handling."""

    def __init__(self):
        self.app_id = os.getenv("GITHUB_APP_ID")
        self.private_key_path = os.getenv("GITHUB_APP_PRIVATE_KEY_PATH")
        self._private_key = None
        # Cache: installation_id -> (token, expires_at_utc)
        self._token_cache: dict[int, tuple[str, datetime]] = {}

        if not self.app_id:
            logger.warning("github_app_id_missing", env_var="GITHUB_APP_ID")

    async def _make_request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        **kwargs: Any,
    ) -> httpx.Response:
        """Make HTTP request with rate limit handling."""
        max_retries = 3

        async with httpx.AsyncClient() as client:
            for attempt in range(max_retries):
                try:
                    resp = await client.request(method, url, headers=headers, **kwargs)

                    # Handle Rate Limiting
                    if resp.status_code in (httpx.codes.FORBIDDEN, httpx.codes.TOO_MANY_REQUESTS):
                        remaining = resp.headers.get("x-ratelimit-remaining")
                        if remaining == "0":
                            reset_time = int(resp.headers.get("x-ratelimit-reset", 0))
                            wait_seconds = max(reset_time - time.time(), 0) + 1

                            if wait_seconds > 60:  # noqa: PLR2004
                                # Fail fast if wait is too long
                                logger.error(
                                    "github_rate_limit_exceeded_long_wait",
                                    wait_seconds=wait_seconds,
                                )
                                resp.raise_for_status()

                            logger.warning("github_rate_limit_hit", wait_seconds=wait_seconds)
                            await asyncio.sleep(wait_seconds)
                            continue

                    resp.raise_for_status()
                    return resp

                except httpx.HTTPStatusError as e:
                    if attempt == max_retries - 1:
                        raise
                    # Only retry server errors or rate limits (if not handled above)
                    if (
                        e.response.status_code < httpx.codes.INTERNAL_SERVER_ERROR
                        and e.response.status_code
                        not in (
                            httpx.codes.FORBIDDEN,
                            httpx.codes.TOO_MANY_REQUESTS,
                        )
                    ):
                        raise
                    await asyncio.sleep(2**attempt)  # Exponential backoff
                except httpx.RequestError:
                    if attempt == max_retries - 1:
                        raise
                    await asyncio.sleep(2**attempt)

            raise RuntimeError("Unreachable")

    def _load_private_key(self) -> str:
        if self._private_key:
            return self._private_key

        if not self.private_key_path:
            raise RuntimeError("GITHUB_APP_PRIVATE_KEY_PATH is not set")

        if not os.path.exists(self.private_key_path):
            # Fallback for local dev if key is missing or env var explicit
            if os.getenv("GITHUB_PRIVATE_KEY_CONTENT"):
                self._private_key = os.getenv("GITHUB_PRIVATE_KEY_CONTENT")
                return self._private_key

            raise FileNotFoundError(f"GitHub App private key not found at {self.private_key_path}")

        with open(self.private_key_path) as f:
            self._private_key = f.read()
        return self._private_key

    def _generate_jwt(self) -> str:
        """Generate JWT for GitHub App authentication."""
        pem = self._load_private_key()
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + (10 * 60), "iss": self.app_id}
        return jwt.encode(payload, pem, algorithm="RS256")

    async def get_installation_id(self, owner: str, repo: str) -> int:
        """Get installation ID for a specific repo."""
        jwt_token = self._generate_jwt()
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github.v3+json",
        }

        resp = await self._make_request(
            "GET", f"https://api.github.com/repos/{owner}/{repo}/installation", headers=headers
        )
        return resp.json()["id"]

    async def get_org_installation_id(self, org: str) -> int:
        """Get installation ID for an organization."""
        jwt_token = self._generate_jwt()
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
        }
        resp = await self._make_request(
            "GET", f"https://api.github.com/orgs/{org}/installation", headers=headers
        )
        return resp.json()["id"]

    async def get_first_org_installation(self) -> dict:
        """Get the first organization installation for this GitHub App.

        Returns:
            Dict with 'org' (organization login) and 'installation_id'
        """
        jwt_token = self._generate_jwt()
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
        }

        resp = await self._make_request(
            "GET",
            "https://api.github.com/app/installations",
            headers=headers,
        )
        installations = resp.json()

        for inst in installations:
            if inst.get("account", {}).get("type") == "Organization":
                return {
                    "org": inst["account"]["login"],
                    "installation_id": inst["id"],
                }

        if installations:
            account = installations[0].get("account", {})
            return {
                "org": account.get("login"),
                "installation_id": installations[0]["id"],
            }

        raise RuntimeError("No GitHub App installations found")

    async def _get_installation_token(self, installation_id: int) -> str:
        """Get or create installation access token with caching."""
        # 1. Check cache
        if installation_id in self._token_cache:
            token, expires_at = self._token_cache[installation_id]
            # Buffer of 60 seconds
            if datetime.now(UTC) < expires_at - timedelta(seconds=60):
                return token

        # 2. Generate new token
        jwt_token = self._generate_jwt()
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
        }

        try:
            resp = await self._make_request(
                "POST",
                f"https://api.github.com/app/installations/{installation_id}/access_tokens",
                headers=headers,
            )
            data = resp.json()
            token = data["token"]

            # Parse expiration: 2016-07-11T22:14:10Z
            expires_at = datetime.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            )

            # 3. Update cache
            self._token_cache[installation_id] = (token, expires_at)

            return token
        except Exception:
            logger.exception("github_app_token_generation_failed", installation_id=installation_id)
            raise

    async def get_org_token(self, org: str) -> str:
        """Get an installation access token for an organization."""
        try:
            installation_id = await self.get_org_installation_id(org)
            return await self._get_installation_token(installation_id)
        except Exception:
            logger.exception("github_app_token_failed", org=org)
            raise

    async def get_token(self, owner: str, repo: str) -> str:
        """Get an installation access token for a specific repo."""
        try:
            installation_id = await self.get_installation_id(owner, repo)
            return await self._get_installation_token(installation_id)
        except Exception:
            logger.exception("github_app_token_failed", owner=owner, repo=repo)
            raise
