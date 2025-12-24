import time
import jwt
import httpx
import os
import logging
from typing import Any

logger = logging.getLogger(__name__)

class GitHubAppClient:
    """Client for authenticated GitHub App interactions."""

    def __init__(self):
        self.app_id = os.getenv("GITHUB_APP_ID")
        self.private_key_path = os.getenv("GITHUB_APP_PRIVATE_KEY_PATH", "/app/keys/github_app.pem")
        self._private_key = None
        
        if not self.app_id:
            logger.warning("GITHUB_APP_ID not set. GitHub App features disabled.")

    def _load_private_key(self) -> str:
        if self._private_key:
            return self._private_key
            
        if not os.path.exists(self.private_key_path):
            raise FileNotFoundError(f"GitHub App private key not found at {self.private_key_path}")
            
        with open(self.private_key_path, "r") as f:
            self._private_key = f.read()
        return self._private_key

    def _generate_jwt(self) -> str:
        """Generate JWT for GitHub App authentication."""
        pem = self._load_private_key()
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + (10 * 60),
            "iss": self.app_id
        }
        return jwt.encode(payload, pem, algorithm="RS256")

    async def get_installation_id(self, owner: str, repo: str) -> int:
        """Get installation ID for a specific repo."""
        jwt_token = self._generate_jwt()
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/installation",
                headers=headers
            )
            # If repo doesn't exist or app not installed, permissions issue likely
            resp.raise_for_status()
            return resp.json()["id"]

    async def get_org_installation_id(self, org: str) -> int:
        """Get installation ID for an organization."""
        jwt_token = self._generate_jwt()
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json"
        }
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.github.com/orgs/{org}/installation",
                headers=headers
            )
            resp.raise_for_status()
            return resp.json()["id"]

    async def get_token(self, owner: str, repo: str) -> str:
        """Get an installation access token for a specific repo."""
        try:
            installation_id = await self.get_installation_id(owner, repo)
            jwt_token = self._generate_jwt()
            headers = {
                "Authorization": f"Bearer {jwt_token}",
                "Accept": "application/vnd.github+json" 
            }
            
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"https://api.github.com/app/installations/{installation_id}/access_tokens",
                    headers=headers
                )
                resp.raise_for_status()
                return resp.json()["token"]
        except Exception as e:
            logger.error(f"Failed to get GitHub App token for {owner}/{repo}: {e}")
            raise
