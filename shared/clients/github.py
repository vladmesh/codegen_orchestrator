import asyncio
import base64
from datetime import UTC, datetime, timedelta
import os
import time
from typing import Any

import httpx
import jwt

from shared.log_config import get_logger
from shared.schemas.github import GitHubRepository

logger = get_logger(__name__)


class WorkflowNotFoundError(RuntimeError):
    """Raised when a GitHub Actions workflow file does not exist in the repository."""


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
        token: str | None = None,
    ) -> None:
        """Set an encrypted repository secret for GitHub Actions.

        Uses libsodium (via pynacl) to encrypt the secret value before
        sending it to GitHub API.

        Args:
            owner: Repository owner
            repo: Repository name
            secret_name: Name of the secret (e.g., DEPLOY_HOST)
            secret_value: Plain text value to encrypt and store
            token: Optional pre-obtained token (e.g. org-level). Falls back to per-repo lookup.
        """
        # Lazy import: pynacl only needed when this method is called
        from nacl import public

        if not token:
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
        token: str | None = None,
    ) -> int:
        """Set multiple repository secrets at once.

        Args:
            owner: Repository owner
            repo: Repository name
            secrets: Dictionary of secret_name -> secret_value
            token: Optional pre-obtained token (e.g. org-level). Falls back to per-repo lookup.

        Returns:
            Number of secrets successfully set
        """
        count = 0
        for name, value in secrets.items():
            try:
                await self.set_repository_secret(owner, repo, name, value, token=token)
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

    async def update_branch_protection(
        self,
        owner: str,
        repo: str,
        branch: str,
        required_checks: list[str],
        require_pr: bool = True,
        enforce_admins: bool = False,
    ) -> None:
        """Set branch protection rules via GitHub API.

        Args:
            owner: Repository owner (org)
            repo: Repository name
            branch: Branch to protect
            required_checks: Status check contexts to require (e.g. ["lint-and-test"])
            require_pr: Require PR for merges
            enforce_admins: Apply rules to admins too

        Raises:
            httpx.HTTPStatusError: On API errors (404 if branch doesn't exist)
        """
        token = await self.get_org_token(owner)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        payload: dict = {
            "required_status_checks": {
                "strict": True,
                "contexts": required_checks,
            },
            "enforce_admins": enforce_admins,
            "restrictions": None,
        }

        if require_pr:
            payload["required_pull_request_reviews"] = {
                "required_approving_review_count": 0,
            }
        else:
            payload["required_pull_request_reviews"] = None

        resp = await self._make_request(
            "PUT",
            f"https://api.github.com/repos/{owner}/{repo}/branches/{branch}/protection",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()

        logger.info(
            "branch_protection_updated",
            owner=owner,
            repo=repo,
            branch=branch,
            checks=required_checks,
            require_pr=require_pr,
        )

    async def enable_repo_auto_merge(self, owner: str, repo: str) -> None:
        """Enable allow_auto_merge repo setting so PRs can use auto-merge."""
        token = await self.get_org_token(owner)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }
        resp = await self._make_request(
            "PATCH",
            f"https://api.github.com/repos/{owner}/{repo}",
            headers=headers,
            json={"allow_auto_merge": True},
        )
        resp.raise_for_status()
        logger.info("repo_auto_merge_enabled", owner=owner, repo=repo)

    async def trigger_workflow_dispatch(
        self,
        owner: str,
        repo: str,
        workflow_file: str,
        ref: str = "main",
        inputs: dict | None = None,
    ) -> bool:
        """Trigger a workflow_dispatch event.

        Args:
            owner: Repository owner
            repo: Repository name
            workflow_file: Workflow filename (e.g. "deploy.yml")
            ref: Git ref to run the workflow on
            inputs: Optional workflow inputs

        Returns:
            True if dispatch was accepted (204)

        Raises:
            httpx.HTTPStatusError: On 404 (workflow not found) or 422 (validation error)
        """
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        payload: dict = {"ref": ref}
        if inputs:
            payload["inputs"] = inputs

        await self._make_request(
            "POST",
            f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_file}/dispatches",
            headers=headers,
            json=payload,
        )

        logger.info(
            "workflow_dispatch_triggered",
            owner=owner,
            repo=repo,
            workflow=workflow_file,
            ref=ref,
        )
        return True

    async def get_latest_workflow_run(
        self,
        owner: str,
        repo: str,
        workflow_file: str = "main.yml",
        branch: str = "main",
        created_after: datetime | None = None,
        head_sha: str | None = None,
    ) -> dict | None:
        """Get the most recent workflow run for a branch.

        Args:
            owner: Repository owner
            repo: Repository name
            workflow_file: Workflow filename (e.g. "ci.yml")
            branch: Branch name to filter by
            created_after: If set, ignore runs created before this time.
                Useful to avoid picking up stale runs after a new push.
            head_sha: If set, only return runs for this exact commit SHA.
                Prevents picking up runs from a different commit (e.g. scaffold).

        Returns:
            Workflow run dict with keys: id, status, conclusion, html_url
            None if no runs found (or all runs are stale)
        """
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        params: dict[str, str | int] = {"branch": branch, "per_page": 1}
        if created_after:
            params["created"] = f">={created_after.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        if head_sha:
            params["head_sha"] = head_sha

        try:
            resp = await self._make_request(
                "GET",
                f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_file}/runs",
                headers=headers,
                params=params,
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == httpx.codes.NOT_FOUND:
                raise WorkflowNotFoundError(
                    f"Workflow '{workflow_file}' not found in {owner}/{repo}. "
                    "The repository may be missing .github/workflows/ — "
                    "scaffold phase likely failed or was skipped."
                ) from e
            raise

        runs = resp.json().get("workflow_runs", [])
        if not runs:
            return None

        run = runs[0]
        return {
            "id": run["id"],
            "status": run["status"],  # queued, in_progress, completed
            "conclusion": run.get("conclusion"),  # success, failure, cancelled
            "html_url": run["html_url"],
            "created_at": run["created_at"],
            "head_sha": run.get("head_sha"),
        }

    async def wait_for_workflow_completion(
        self,
        owner: str,
        repo: str,
        workflow_file: str = "main.yml",
        branch: str = "main",
        timeout_seconds: int = 600,
        poll_interval: int = 15,
        created_after: datetime | None = None,
        head_sha: str | None = None,
    ) -> dict:
        """Wait for the latest workflow run to complete.

        Args:
            owner: Repository owner
            repo: Repository name
            workflow_file: Workflow filename to monitor
            branch: Branch name
            timeout_seconds: Max wait time
            poll_interval: Seconds between polls
            created_after: If set, only consider runs created after this time.
                Prevents picking up stale runs from before a new push.
            head_sha: If set, only consider runs for this exact commit SHA.

        Returns:
            Final workflow run state

        Raises:
            TimeoutError: If workflow doesn't complete within timeout
            RuntimeError: If workflow fails
        """
        start = datetime.now(UTC)

        while True:
            elapsed = (datetime.now(UTC) - start).total_seconds()
            if elapsed > timeout_seconds:
                raise TimeoutError(
                    f"Workflow {workflow_file} did not complete within {timeout_seconds}s"
                )

            run = await self.get_latest_workflow_run(
                owner,
                repo,
                workflow_file,
                branch,
                created_after=created_after,
                head_sha=head_sha,
            )

            if not run:
                logger.info("workflow_not_found_waiting", workflow=workflow_file)
                await asyncio.sleep(poll_interval)
                continue

            if run["status"] == "completed":
                if run["conclusion"] == "success":
                    logger.info(
                        "workflow_completed_success",
                        workflow=workflow_file,
                        run_id=run["id"],
                    )
                    return run
                else:
                    raise RuntimeError(
                        f"Workflow {workflow_file} failed: {run['conclusion']}. "
                        f"See: {run['html_url']}"
                    )

            logger.info(
                "workflow_in_progress",
                workflow=workflow_file,
                status=run["status"],
                elapsed_sec=int(elapsed),
            )
            await asyncio.sleep(poll_interval)

    async def get_workflow_failure_logs(
        self,
        owner: str,
        repo: str,
        run_id: int,
    ) -> str:
        """Fetch failure details from a workflow run.

        Gets failed job names and step names from the GitHub Actions API,
        providing context about what failed in CI.

        Args:
            owner: Repository owner
            repo: Repository name
            run_id: Workflow run ID

        Returns:
            Formatted string describing what failed
        """
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        resp = await self._make_request(
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
            headers=headers,
        )
        jobs = resp.json().get("jobs", [])

        lines = []
        for job in jobs:
            if job.get("conclusion") != "failure":
                continue
            lines.append(f"Job '{job['name']}' failed:")
            for step in job.get("steps", []):
                if step.get("conclusion") == "failure":
                    lines.append(f"  Step '{step['name']}' failed")

        if not lines:
            return f"Workflow run {run_id} failed (no job details available)"

        return "\n".join(lines)

    async def rerun_failed_jobs(self, owner: str, repo: str, run_id: int) -> bool:
        """Rerun only the failed jobs in a workflow run.

        Args:
            owner: Repository owner
            repo: Repository name
            run_id: Workflow run ID

        Returns:
            True if rerun was accepted (201)

        Raises:
            httpx.HTTPStatusError: On 403 (insufficient permissions) or other errors
        """
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        await self._make_request(
            "POST",
            f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs",
            headers=headers,
        )

        logger.info(
            "workflow_rerun_failed_jobs_triggered",
            owner=owner,
            repo=repo,
            run_id=run_id,
        )
        return True

    async def wait_for_run_completion(
        self,
        owner: str,
        repo: str,
        run_id: int,
        timeout_seconds: int = 600,
        poll_interval: int = 15,
    ) -> dict:
        """Wait for a specific workflow run to complete.

        Unlike wait_for_workflow_completion, this polls a known run_id directly.
        Useful after rerun_failed_jobs where the run_id stays the same but
        created_at doesn't change (so the created_after filter won't find it).

        Args:
            owner: Repository owner
            repo: Repository name
            run_id: Workflow run ID to poll
            timeout_seconds: Max wait time
            poll_interval: Seconds between polls

        Returns:
            Dict with {id, status, conclusion, html_url} on success

        Raises:
            RuntimeError: If run completes with non-success conclusion
            TimeoutError: If run doesn't complete within timeout
        """
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        start = datetime.now(UTC)

        while True:
            elapsed = (datetime.now(UTC) - start).total_seconds()
            if elapsed > timeout_seconds:
                raise TimeoutError(
                    f"Workflow run {run_id} did not complete within {timeout_seconds}s"
                )

            resp = await self._make_request(
                "GET",
                f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}",
                headers=headers,
            )
            run = resp.json()

            if run["status"] == "completed":
                result = {
                    "id": run["id"],
                    "status": run["status"],
                    "conclusion": run.get("conclusion"),
                    "html_url": run["html_url"],
                }
                if run.get("conclusion") == "success":
                    logger.info(
                        "workflow_run_completed_success",
                        run_id=run_id,
                    )
                    return result
                else:
                    raise RuntimeError(
                        f"Workflow run {run_id} failed: {run.get('conclusion')}. "
                        f"See: {run['html_url']}"
                    )

            logger.info(
                "workflow_run_in_progress",
                run_id=run_id,
                status=run["status"],
                elapsed_sec=int(elapsed),
            )
            await asyncio.sleep(poll_interval)

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

    # --- Pull Request methods ---

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str = "",
    ) -> dict:
        """Create a pull request.

        If a PR already exists for the same head→base, returns the existing PR.

        Returns:
            PR dict with number, html_url, node_id, etc.
        """
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        try:
            resp = await self._make_request(
                "POST",
                f"https://api.github.com/repos/{owner}/{repo}/pulls",
                headers=headers,
                json={"head": head, "base": base, "title": title, "body": body},
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code != httpx.codes.UNPROCESSABLE_ENTITY:
                raise
            # PR already exists — find and return it
            logger.info("pr_already_exists", owner=owner, repo=repo, head=head)
            list_resp = await self._make_request(
                "GET",
                f"https://api.github.com/repos/{owner}/{repo}/pulls",
                headers=headers,
                params={"head": f"{owner}:{head}", "base": base, "state": "open"},
            )
            prs = list_resp.json()
            if prs:
                return prs[0]
            raise RuntimeError(
                f"PR creation returned 422 but no existing PR found for {head}→{base}"
            ) from e

        pr = resp.json()
        logger.info(
            "pr_created",
            owner=owner,
            repo=repo,
            pr_number=pr["number"],
            head=head,
            base=base,
        )
        return pr

    async def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict:
        """Fetch a single pull request by number."""
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }
        resp = await self._make_request(
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=headers,
        )
        return resp.json()

    async def enable_auto_merge(
        self,
        owner: str,
        repo: str,
        pr_node_id: str,
        merge_method: str = "MERGE",
    ) -> bool:
        """Enable auto-merge on a pull request via GraphQL.

        Args:
            owner: Repository owner (for token resolution)
            repo: Repository name (for token resolution)
            pr_node_id: The GraphQL node_id of the pull request
            merge_method: MERGE, SQUASH, or REBASE

        Returns:
            True if auto-merge was enabled, False if not allowed.
        """
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"bearer {token}",
            "Accept": "application/vnd.github+json",
        }

        query = """
        mutation EnableAutoMerge($pullRequestId: ID!, $mergeMethod: PullRequestMergeMethod!) {
            enablePullRequestAutoMerge(input: {
                pullRequestId: $pullRequestId,
                mergeMethod: $mergeMethod
            }) {
                pullRequest {
                    number
                    autoMergeRequest { mergeMethod }
                }
            }
        }
        """

        resp = await self._make_request(
            "POST",
            "https://api.github.com/graphql",
            headers=headers,
            json={
                "query": query,
                "variables": {"pullRequestId": pr_node_id, "mergeMethod": merge_method},
            },
        )

        data = resp.json()
        if "errors" in data:
            logger.warning(
                "auto_merge_failed",
                owner=owner,
                repo=repo,
                errors=data["errors"],
            )
            return False

        logger.info("auto_merge_enabled", owner=owner, repo=repo, pr_node_id=pr_node_id)
        return True

    async def merge_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        merge_method: str = "merge",
    ) -> dict:
        """Merge a pull request.

        Args:
            merge_method: "merge", "squash", or "rebase"

        Returns:
            Merge result dict with sha, merged fields.
        """
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        resp = await self._make_request(
            "PUT",
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/merge",
            headers=headers,
            json={"merge_method": merge_method},
        )
        resp.raise_for_status()

        result = resp.json()
        logger.info(
            "pr_merged",
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            sha=result.get("sha"),
        )
        return result

    async def list_pull_requests(
        self,
        owner: str,
        repo: str,
        head: str | None = None,
        base: str | None = None,
        state: str = "closed",
    ) -> list[dict]:
        """List pull requests with optional filters.

        Args:
            head: Filter by head branch (format: "owner:branch" or just "branch").
            base: Filter by base branch (e.g. "main").
            state: PR state: open, closed, all.
        """
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }
        params: dict[str, str] = {"state": state}
        if head:
            # GitHub API expects "owner:branch" format
            params["head"] = head if ":" in head else f"{owner}:{head}"
        if base:
            params["base"] = base

        resp = await self._make_request(
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            headers=headers,
            params=params,
        )
        return resp.json()

    async def close_pull_request(self, owner: str, repo: str, pr_number: int) -> dict:
        """Close a pull request without merging."""
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        resp = await self._make_request(
            "PATCH",
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=headers,
            json={"state": "closed"},
        )
        resp.raise_for_status()

        result = resp.json()
        logger.info("pr_closed", owner=owner, repo=repo, pr_number=pr_number)
        return result
