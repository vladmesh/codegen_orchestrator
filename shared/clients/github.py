import base64
import os
import time

import httpx
import jwt

from shared.logging_config import get_logger
from shared.schemas.github import GitHubRepository

logger = get_logger(__name__)


class GitHubAppClient:
    """Client for authenticated GitHub App interactions."""

    def __init__(self):
        self.app_id = os.getenv("GITHUB_APP_ID")
        self.private_key_path = os.getenv("GITHUB_APP_PRIVATE_KEY_PATH", "/app/keys/github_app.pem")
        self._private_key = None

        if not self.app_id:
            logger.warning("github_app_id_missing", env_var="GITHUB_APP_ID")

    def _load_private_key(self) -> str:
        if self._private_key:
            return self._private_key

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

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/installation", headers=headers
            )
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

        Returns:
            Dict with 'org' (organization login) and 'installation_id'
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

    async def get_org_token(self, org: str) -> str:
        """Get an installation access token for an organization."""
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
        except Exception:
            logger.exception("github_app_token_failed", org=org)
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
        except Exception:
            logger.exception("github_app_token_failed", owner=owner, repo=repo)
            raise

    async def create_repo(
        self, org: str, name: str, description: str = "", private: bool = True
    ) -> GitHubRepository:
        """Create a new repository in the organization."""
        token = await self.get_org_token(org)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        payload = {
            "name": name,
            "description": description,
            "private": private,
            "auto_init": True,
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.github.com/orgs/{org}/repos",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info(
                "github_repo_created",
                org=org,
                name=name,
                repo_url=data.get("html_url"),
            )
            return GitHubRepository.model_validate(data)

    async def list_org_repos(self, org: str) -> list[GitHubRepository]:
        """List all repositories in the organization."""
        token = await self.get_org_token(org)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        repos = []
        page = 1
        per_page = 100

        async with httpx.AsyncClient() as client:
            while True:
                resp = await client.get(
                    f"https://api.github.com/orgs/{org}/repos",
                    params={"type": "all", "per_page": per_page, "page": page},
                    headers=headers,
                )
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break
                repos.extend([GitHubRepository.model_validate(r) for r in batch])
                if len(batch) < per_page:
                    break
                page += 1

        return repos

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
        """Get contents of a file from a repository."""
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
            logger.warning(
                "github_file_fetch_failed",
                owner=owner,
                repo=repo,
                path=path,
                error=str(e),
            )
            return None

    async def list_repo_files(
        self, owner: str, repo: str, path: str = "", ref: str = "main"
    ) -> list[str]:
        """List files in a repository directory."""
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
            logger.warning(
                "github_list_files_failed",
                owner=owner,
                repo=repo,
                path=path,
                error=str(e),
            )
            return []

    async def create_or_update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str = "main",
    ) -> dict:
        """Create or update a file in the repository."""
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        # First, try to get existing file SHA to support updates
        sha = None
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
                    headers=headers,
                    params={"ref": branch},
                )
                if resp.status_code == httpx.codes.OK:
                    sha = resp.json().get("sha")
        except Exception:  # noqa: S110
            pass

        # Prepare payload
        content_b64 = base64.b64encode(content.encode()).decode()
        payload = {
            "message": message,
            "content": content_b64,
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha

        # Create/Update file
        async with httpx.AsyncClient() as client:
            resp = await client.put(
                f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

            logger.info(
                "github_file_updated",
                owner=owner,
                repo=repo,
                path=path,
                action="update" if sha else "create",
                sha=data["content"]["sha"],
            )
            return data["content"]

    async def set_repository_secret(
        self,
        owner: str,
        repo: str,
        secret_name: str,
        secret_value: str,
    ) -> None:
        """Set an encrypted repository secret for GitHub Actions.

        Uses libsodium (via pynacl) to encrypt the secret value before
        sending it to GitHub API.

        Args:
            owner: Repository owner
            repo: Repository name
            secret_name: Name of the secret (e.g., DEPLOY_HOST)
            secret_value: Plain text value to encrypt and store
        """
        # Lazy import: pynacl only needed when this method is called
        from nacl import public

        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        async with httpx.AsyncClient() as client:
            # 1. Get repository public key for encryption
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/public-key",
                headers=headers,
            )
            resp.raise_for_status()
            key_data = resp.json()
            public_key_b64 = key_data["key"]
            key_id = key_data["key_id"]

            # 2. Encrypt the secret using libsodium sealed box
            public_key_bytes = base64.b64decode(public_key_b64)
            pub_key = public.PublicKey(public_key_bytes)
            sealed_box = public.SealedBox(pub_key)
            encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
            encrypted_b64 = base64.b64encode(encrypted).decode("utf-8")

            # 3. Create or update the secret
            resp = await client.put(
                f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/{secret_name}",
                headers=headers,
                json={
                    "encrypted_value": encrypted_b64,
                    "key_id": key_id,
                },
            )
            resp.raise_for_status()

            logger.info(
                "github_secret_set",
                owner=owner,
                repo=repo,
                secret_name=secret_name,
            )

    async def set_repository_secrets(
        self,
        owner: str,
        repo: str,
        secrets: dict[str, str],
    ) -> int:
        """Set multiple repository secrets at once.

        Args:
            owner: Repository owner
            repo: Repository name
            secrets: Dictionary of secret_name -> secret_value

        Returns:
            Number of secrets successfully set
        """
        count = 0
        for name, value in secrets.items():
            try:
                await self.set_repository_secret(owner, repo, name, value)
                count += 1
            except Exception as e:
                logger.error(
                    "github_secret_set_failed",
                    owner=owner,
                    repo=repo,
                    secret_name=name,
                    error=str(e),
                )
        return count
