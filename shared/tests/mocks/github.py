from datetime import UTC, datetime
from typing import Any

import httpx

from shared.logging_config import get_logger
from shared.schemas.github import GitHubRepository

logger = get_logger(__name__)


class MockGitHubClient:
    """In-memory GitHub mock for unit/integration tests."""

    def __init__(self, settings: Any | None = None):
        self.settings = settings
        # State
        self.repos: dict[str, GitHubRepository] = {}  # name -> GitHubRepository
        self.files: dict[str, dict[str, str]] = {}  # repo_name -> { path -> content }
        self.secrets: dict[str, dict[str, str]] = {}  # repo_name -> { key -> value }

        # Behavior Configuration
        self.should_fail: bool = False
        self.fail_exception: Exception | None = None

    def _check_failure(self):
        if self.should_fail:
            raise self.fail_exception or RuntimeError("Simulated Failure")

    async def get_org_token(self, org: str) -> str:
        self._check_failure()
        return "mock-org-token"

    async def get_token(self, owner: str, repo: str) -> str:
        self._check_failure()
        return "mock-repo-token"

    async def create_repo(
        self, org: str, name: str, description: str = "", private: bool = True
    ) -> GitHubRepository:
        self._check_failure()

        full_name = f"{org}/{name}"

        # Check if repo exists in our mock state
        # Note: In real GitHub, if repo exists, it returns 422.
        # But our client handles it by checking or catching.
        # We'll simulate success for simplicity unless specifically testing collision logic,
        # or we could stick to the client's expectation.

        repo = GitHubRepository(
            id=len(self.repos) + 1,
            name=name,
            full_name=full_name,
            private=private,
            html_url=f"https://github.com/{full_name}",
            clone_url=f"https://github.com/{full_name}.git",
            default_branch="main",
            description=description,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self.repos[name] = repo
        self.files[name] = {}
        self.secrets[name] = {}

        logger.info("mock_github_repo_created", name=name)
        return repo

    async def get_repo(self, owner: str, repo: str) -> GitHubRepository:
        self._check_failure()
        if repo not in self.repos:
            # Simulate httpx.HTTPStatusError for 404
            request = httpx.Request("GET", f"https://api.github.com/repos/{owner}/{repo}")
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("Not Found", request=request, response=response)
        return self.repos[repo]

    async def list_org_repos(self, org: str) -> list[GitHubRepository]:
        self._check_failure()
        return list(self.repos.values())

    async def get_file_contents(
        self, owner: str, repo: str, path: str, ref: str = "main"
    ) -> str | None:
        self._check_failure()
        if repo not in self.repos:
            return None

        return self.files[repo].get(path)

    async def list_repo_files(
        self, owner: str, repo: str, path: str = "", ref: str = "main"
    ) -> list[str]:
        self._check_failure()
        if repo not in self.repos:
            return []

        # Very simple listing logic
        files = []
        for file_path in self.files[repo].keys():
            if file_path.startswith(path):
                # Just return the filenames/paths
                files.append(file_path)
        return files

    async def create_or_update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str = "main",
    ) -> dict:
        self._check_failure()

        if repo not in self.repos:
            # Auto-create repo if missing for convenience? No, strict mock.
            # Simulate 404
            request = httpx.Request(
                "PUT", f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
            )
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("Repo Not Found", request=request, response=response)

        self.files[repo][path] = content

        return {
            "name": path.split("/")[-1],
            "path": path,
            "sha": f"mock-sha-{len(self.files[repo])}",
            "html_url": f"https://github.com/{owner}/{repo}/blob/{branch}/{path}",
        }

    async def set_repository_secret(
        self,
        owner: str,
        repo: str,
        secret_name: str,
        secret_value: str,
    ) -> None:
        self._check_failure()
        if repo in self.secrets:
            self.secrets[repo][secret_name] = secret_value

    async def set_repository_secrets(
        self,
        owner: str,
        repo: str,
        secrets: dict[str, str],
    ) -> int:
        self._check_failure()
        count = 0
        if repo in self.secrets:
            for k, v in secrets.items():
                self.secrets[repo][k] = v
                count += 1
        return count

    async def provision_project_repo(
        self,
        name: str,
        description: str = "",
        project_spec: dict | None = None,
        secrets: dict[str, str] | None = None,
    ) -> GitHubRepository:
        self._check_failure()

        # 1. Create repo
        # Since this is a mock, we assume org is provided or hardcoded
        org = "mock-org"
        repo_name = name.lower().replace(" ", "-").replace("_", "-")

        if repo_name in self.repos:
            # Idempotency check logic from real client
            repo = self.repos[repo_name]
        else:
            repo = await self.create_repo(org, repo_name, description)

        # 2. Add .project.yaml
        if project_spec:
            import yaml

            config_content = yaml.dump(project_spec, default_flow_style=False)
            await self.create_or_update_file(
                org, repo_name, ".project.yaml", config_content, "chore: init"
            )

        # 3. Secrets
        if secrets:
            await self.set_repository_secrets(org, repo_name, secrets)

        return repo
