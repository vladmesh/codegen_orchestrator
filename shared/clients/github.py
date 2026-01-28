import asyncio
import base64
from datetime import UTC, datetime, timedelta
import os
import time
from typing import Any

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

        resp = await self._make_request(
            "POST",
            f"https://api.github.com/orgs/{org}/repos",
            headers=headers,
            json=payload,
        )
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

        while True:
            resp = await self._make_request(
                "GET",
                f"https://api.github.com/orgs/{org}/repos",
                params={"type": "all", "per_page": per_page, "page": page},
                headers=headers,
            )
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

        resp = await self._make_request(
            "GET", f"https://api.github.com/repos/{owner}/{repo}", headers=headers
        )
        return GitHubRepository.model_validate(resp.json())

    async def delete_repo(self, owner: str, repo: str) -> bool:
        """Delete a repository.

        Requires the GitHub App to have 'administration' permission with 'write' access.

        Args:
            owner: Repository owner (org or user)
            repo: Repository name

        Returns:
            True if deleted successfully, False if repo not found
        """
        try:
            token = await self.get_token(owner, repo)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == httpx.codes.NOT_FOUND:
                logger.info("github_repo_not_found_skip_delete", owner=owner, repo=repo)
                return False
            raise

        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        try:
            await self._make_request(
                "DELETE", f"https://api.github.com/repos/{owner}/{repo}", headers=headers
            )
            logger.info("github_repo_deleted", owner=owner, repo=repo)
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == httpx.codes.NOT_FOUND:
                logger.info("github_repo_not_found_skip_delete", owner=owner, repo=repo)
                return False
            raise

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

            resp = await self._make_request(
                "GET",
                f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
                headers=headers,
                params={"ref": ref},
            )
            return resp.text
        except httpx.HTTPStatusError as e:
            if e.response.status_code == httpx.codes.NOT_FOUND:
                return None
            raise
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

            resp = await self._make_request(
                "GET",
                f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
                headers=headers,
                params={"ref": ref},
            )
            return [item["name"] for item in resp.json()]
        except httpx.HTTPStatusError as e:
            if e.response.status_code == httpx.codes.NOT_FOUND:
                return []
            raise
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
            resp = await self._make_request(
                "GET",
                f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
                headers=headers,
                params={"ref": branch},
            )
            sha = resp.json().get("sha")
        except httpx.HTTPStatusError as e:
            if e.response.status_code != httpx.codes.NOT_FOUND:
                raise
        except Exception as e:
            # File might not exist or other error, proceed to create
            logger.debug("github_file_check_failed", error=str(e))

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
        resp = await self._make_request(
            "PUT",
            f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
            headers=headers,
            json=payload,
        )
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

        # 1. Get repository public key for encryption
        resp = await self._make_request(
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/public-key",
            headers=headers,
        )
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
        await self._make_request(
            "PUT",
            f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/{secret_name}",
            headers=headers,
            json={
                "encrypted_value": encrypted_b64,
                "key_id": key_id,
            },
        )

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

    async def provision_project_repo(
        self,
        name: str,
        description: str = "",
        project_spec: dict | None = None,
        secrets: dict[str, str] | None = None,
    ) -> GitHubRepository:
        """Create repo with initial config and secrets.

        Org is auto-detected from GitHub App installation.

        Args:
            name: Repository name (will be sanitized to kebab-case)
            description: Repository description
            project_spec: Project specification to save as .project.yaml
            secrets: Secrets to set in GitHub Actions (e.g., TELEGRAM_TOKEN)

        Returns:
            Created repository info
        """
        # 1. Auto-detect org from GitHub App installation
        installation = await self.get_first_org_installation()
        org = installation["org"]

        # 2. Sanitize repo name
        repo_name = name.lower().replace(" ", "-").replace("_", "-")

        # 3. Create repository
        try:
            repo = await self.create_repo(org, repo_name, description, private=True)
        except httpx.HTTPStatusError as e:
            # Idempotency: Use existing repo if it already exists
            # GitHub API returns 422 Unprocessable Entity for existing repos
            if e.response.status_code == httpx.codes.UNPROCESSABLE_ENTITY:
                logger.info("github_repo_already_exists_using_existing", org=org, repo=repo_name)
                repo = await self.get_repo(org, repo_name)
            else:
                raise e
        except Exception as e:
            if "422" in str(e):  # Fallback for non-HTTPStatusError exceptions if any
                logger.info(
                    "github_repo_already_exists_using_existing_fallback", org=org, repo=repo_name
                )
                repo = await self.get_repo(org, repo_name)
            else:
                raise e

        # 4. Add .project.yaml if spec provided
        if project_spec:
            import yaml

            config_content = yaml.dump(project_spec, default_flow_style=False, allow_unicode=True)
            await self.create_or_update_file(
                owner=org,
                repo=repo_name,
                path=".project.yaml",
                content=config_content,
                message="chore: add project configuration",
            )

        # 5. Set secrets if provided
        if secrets:
            await self.set_repository_secrets(org, repo_name, secrets)

        logger.info(
            "project_repo_provisioned",
            org=org,
            repo=repo_name,
            secrets_count=len(secrets) if secrets else 0,
        )

        return repo
