import logging
import os
import time

import httpx
import jwt

from shared.schemas import GitHubRepository

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

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/installation", headers=headers
            )
            # If repo doesn't exist or app not installed, permissions issue likely
            resp.raise_for_status()
            return resp.json()["id"]

    async def get_org_installation_id(self, org: str) -> int:
        """Get installation ID for an organization."""
        jwt_token = self._generate_jwt()
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
        }
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.github.com/orgs/{org}/installation", headers=headers
            )
            resp.raise_for_status()
            return resp.json()["id"]

    async def get_first_org_installation(self) -> dict:
        """Get the first organization installation for this GitHub App.

        Returns installation info including org name and installation ID.
        Useful when the app is installed on a single organization.

        Returns:
            Dict with 'org' (organization login) and 'installation_id'

        Raises:
            RuntimeError: If no organization installations found
        """
        jwt_token = self._generate_jwt()
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
        }

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.github.com/app/installations",
                headers=headers,
            )
            resp.raise_for_status()
            installations = resp.json()

        # Find first organization installation
        for inst in installations:
            if inst.get("account", {}).get("type") == "Organization":
                return {
                    "org": inst["account"]["login"],
                    "installation_id": inst["id"],
                }

        # Fallback to first installation (could be user account)
        if installations:
            account = installations[0].get("account", {})
            return {
                "org": account.get("login"),
                "installation_id": installations[0]["id"],
            }

        raise RuntimeError("No GitHub App installations found")

    async def get_org_token(self, org: str) -> str:
        """Get an installation access token for an organization.

        This token can be used to create repositories and perform other org-level operations.
        """
        try:
            installation_id = await self.get_org_installation_id(org)
            jwt_token = self._generate_jwt()
            headers = {
                "Authorization": f"Bearer {jwt_token}",
                "Accept": "application/vnd.github+json",
            }

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"https://api.github.com/app/installations/{installation_id}/access_tokens",
                    headers=headers,
                )
                resp.raise_for_status()
                return resp.json()["token"]
        except Exception as e:
            logger.error(f"Failed to get GitHub App token for org {org}: {e}")
            raise

    async def get_token(self, owner: str, repo: str) -> str:
        """Get an installation access token for a specific repo."""
        try:
            installation_id = await self.get_installation_id(owner, repo)
            jwt_token = self._generate_jwt()
            headers = {
                "Authorization": f"Bearer {jwt_token}",
                "Accept": "application/vnd.github+json",
            }

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"https://api.github.com/app/installations/{installation_id}/access_tokens",
                    headers=headers,
                )
                resp.raise_for_status()
                return resp.json()["token"]
        except Exception as e:
            logger.error(f"Failed to get GitHub App token for {owner}/{repo}: {e}")
            raise

    async def create_repo(
        self, org: str, name: str, description: str = "", private: bool = True
    ) -> GitHubRepository:
        """Create a new repository in the organization.

        Args:
            org: Organization name
            name: Repository name
            description: Repository description
            private: Whether the repository should be private

        Returns:
            Created repository data
        """
        token = await self.get_org_token(org)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        payload = {
            "name": name,
            "description": description,
            "private": private,
            "auto_init": True,  # Create with README
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.github.com/orgs/{org}/repos",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info(f"Created repository: {data['html_url']}")
            return GitHubRepository.model_validate(data)

    async def get_repo(self, owner: str, repo: str) -> GitHubRepository:
        """Get repository information."""
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        async with httpx.AsyncClient() as client:
            resp = await client.get(f"https://api.github.com/repos/{owner}/{repo}", headers=headers)
            resp.raise_for_status()
            return GitHubRepository.model_validate(resp.json())

    async def get_file_contents(
        self, owner: str, repo: str, path: str, ref: str = "main"
    ) -> str | None:
        """Get contents of a file from a repository.

        Args:
            owner: Repository owner (org or user)
            repo: Repository name
            path: Path to file in repo (e.g., '.env.example')
            ref: Branch or commit ref (default 'main')

        Returns:
            File contents as string, or None if file doesn't exist.
        """
        try:
            token = await self.get_token(owner, repo)
            headers = {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.raw+json",
            }

            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
                    headers=headers,
                    params={"ref": ref},
                )
                if resp.status_code == httpx.codes.NOT_FOUND:
                    return None
                resp.raise_for_status()
                return resp.text
        except Exception as e:
            logger.warning(f"Failed to get file {path} from {owner}/{repo}: {e}")
            return None

    async def list_repo_files(
        self, owner: str, repo: str, path: str = "", ref: str = "main"
    ) -> list[str]:
        """List files in a repository directory.

        Args:
            owner: Repository owner
            repo: Repository name
            path: Directory path (empty for root)
            ref: Branch or commit ref

        Returns:
            List of file/directory names.
        """
        try:
            token = await self.get_token(owner, repo)
            headers = {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            }

            async with httpx.AsyncClient() as client:
                url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
                resp = await client.get(url, headers=headers, params={"ref": ref})
                if resp.status_code == httpx.codes.NOT_FOUND:
                    return []
                resp.raise_for_status()
                return [item["name"] for item in resp.json()]
        except Exception as e:
            logger.warning(f"Failed to list files in {owner}/{repo}/{path}: {e}")
            return []
