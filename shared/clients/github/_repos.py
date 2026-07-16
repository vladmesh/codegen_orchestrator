import base64

import httpx

from shared.log_config import get_logger
from shared.schemas.github import GitHubRepository

logger = get_logger(__name__)


class ReposMixin:
    """Repository CRUD and file operations."""

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
                # Repo-scoped installation not found — fall back to org-level token.
                # This happens when the GitHub App isn't installed on the specific repo
                # (e.g. newly created repos before installation binding).
                logger.info("github_repo_token_fallback_to_org", owner=owner, repo=repo)
                token = await self.get_org_token(owner)
            else:
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

    async def list_repo_files_recursive(
        self, owner: str, repo: str, ref: str = "main"
    ) -> list[str]:
        """List file paths in a repository at ``ref``.

        The contents endpoint only lists one directory level.  Callers that
        consume owner-maintained files, such as environment-contract fragments,
        need the complete tree rather than a guessed set of directories.
        """
        try:
            token = await self.get_token(owner, repo)
            headers = {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            }
            response = await self._make_request(
                "GET",
                f"https://api.github.com/repos/{owner}/{repo}/git/trees/{ref}",
                headers=headers,
                params={"recursive": "1"},
            )
            payload = response.json()
            if payload.get("truncated"):
                raise RuntimeError("GitHub repository tree response is truncated")
            return sorted(
                item["path"]
                for item in payload.get("tree", [])
                if item.get("type") == "blob" and isinstance(item.get("path"), str)
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code == httpx.codes.NOT_FOUND:
                return []
            raise

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
