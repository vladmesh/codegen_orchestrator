# Shared: GitHub Client

**Location:** `shared/clients/github.py`
**Config:** `shared/config/github.py`

## 1. Philosophy

GitHub App — единая точка интеграции с GitHub для всего проекта.

> **Rule #1:** Организация указывается явно через `GITHUB_ORG`. Никакого auto-detect.
> **Rule #2:** Один GitHub App, установленный на все нужные организации.
> **Rule #3:** Все сервисы используют `GitHubAppClient` из shared. Никакого дублирования.

## 2. Configuration

### 2.1 Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_ORG` | **Yes** | Target organization name |
| `GITHUB_APP_ID` | Yes | GitHub App numeric ID |
| `GITHUB_APP_PRIVATE_KEY_PATH` | Yes | Path to PEM file |

### 2.2 Settings Class

```python
# shared/config/github.py

from pydantic_settings import BaseSettings

class GitHubSettings(BaseSettings):
    """GitHub App configuration."""

    org: str                                    # REQUIRED, no default
    app_id: int
    private_key_path: str = "/app/keys/github_app.pem"

    # API settings
    api_base_url: str = "https://api.github.com"
    token_ttl_seconds: int = 3600               # Cache tokens for 1 hour

    class Config:
        env_prefix = "GITHUB_"


def get_github_settings() -> GitHubSettings:
    """Get GitHub settings. Raises if not configured."""
    return GitHubSettings()
```

### 2.3 Per-Environment Values

| Environment | `GITHUB_ORG` | Notes |
|-------------|--------------|-------|
| **Production** | `project-factory` | Main org |
| **Development** | `project-factory` | Same as prod (careful!) |
| **Tests (integration)** | N/A | Use MockGitHubClient |
| **Tests (e2e)** | `project-factory-test` | Isolated test org |

## 3. Client Interface

### 3.1 GitHubAppClient

```python
# shared/clients/github.py

class GitHubAppClient:
    """GitHub App client for all GitHub operations."""

    def __init__(self, settings: GitHubSettings | None = None):
        self.settings = settings or get_github_settings()
        self._token_cache: dict[str, tuple[str, datetime]] = {}

    # === Authentication ===

    async def get_org_token(self) -> str:
        """Get installation token for configured org."""
        ...

    async def get_repo_token(self, repo: str) -> str:
        """Get installation token for specific repo."""
        ...

    # === Repository Operations ===

    async def create_repo(
        self,
        name: str,
        description: str = "",
        private: bool = True,
    ) -> Repository:
        """Create repository in configured org."""
        ...

    async def get_repo(self, name: str) -> Repository:
        """Get repository by name (in configured org)."""
        ...

    async def list_repos(self) -> list[Repository]:
        """List all repositories in configured org."""
        ...

    async def delete_repo(self, name: str) -> None:
        """Delete repository. USE WITH CAUTION."""
        ...

    # === File Operations ===

    async def get_file(
        self,
        repo: str,
        path: str,
        ref: str = "main",
    ) -> FileContent:
        """Get file content from repository."""
        ...

    async def create_or_update_file(
        self,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str = "main",
    ) -> str:  # Returns commit SHA
        """Create or update file in repository."""
        ...

    async def list_files(
        self,
        repo: str,
        path: str = "",
        ref: str = "main",
    ) -> list[FileItem]:
        """List files in directory."""
        ...

    # === Secrets ===

    async def set_secret(
        self,
        repo: str,
        name: str,
        value: str,
    ) -> None:
        """Set GitHub Actions secret (encrypted)."""
        ...

    async def set_secrets(
        self,
        repo: str,
        secrets: dict[str, str],
    ) -> None:
        """Set multiple secrets. Continues on individual failures."""
        ...

    # === Provisioning (high-level) ===

    async def provision_repo(
        self,
        name: str,
        description: str,
        spec: dict,
        secrets: dict[str, str] | None = None,
    ) -> Repository:
        """
        One-shot repo provisioning:
        1. Create repo
        2. Add .project.yaml with spec
        3. Set secrets
        """
        ...
```

### 3.2 Data Classes

```python
# shared/schemas/github.py

from pydantic import BaseModel

class Repository(BaseModel):
    id: int
    name: str
    full_name: str              # "org/repo"
    html_url: str
    clone_url: str
    default_branch: str = "main"
    private: bool = True

class FileContent(BaseModel):
    path: str
    content: str                # Decoded content
    sha: str                    # For updates
    encoding: str = "utf-8"

class FileItem(BaseModel):
    name: str
    path: str
    type: Literal["file", "dir"]
    sha: str
```

## 4. Usage Examples

### 4.1 In API Service

```python
# services/api/src/routers/projects.py

from shared.clients.github import GitHubAppClient

@router.post("/projects")
async def create_project(data: ProjectCreate):
    github = GitHubAppClient()

    repo = await github.provision_repo(
        name=data.name,
        description=data.description,
        spec=data.spec.model_dump(),
        secrets=data.secrets,
    )

    project = Project(
        name=data.name,
        repository_url=repo.html_url,
        github_repo_id=repo.id,
    )
    # ... save to DB
```

### 4.2 In LangGraph Tools

```python
# services/langgraph/src/tools/github.py

from shared.clients.github import GitHubAppClient

@tool
async def create_github_repo(name: str, description: str) -> dict:
    """Create a new GitHub repository."""
    github = GitHubAppClient()
    repo = await github.create_repo(name, description)
    return {
        "name": repo.name,
        "url": repo.html_url,
        "clone_url": repo.clone_url,
    }

@tool
async def get_github_token(repo_name: str) -> str:
    """Get token for git operations."""
    github = GitHubAppClient()
    return await github.get_repo_token(repo_name)
```

### 4.3 In Scaffolder

```python
# services/scaffolder/src/main.py

from shared.clients.github import GitHubAppClient

async def clone_and_scaffold(repo_name: str, template: str):
    github = GitHubAppClient()
    token = await github.get_repo_token(repo_name)

    clone_url = f"https://x-access-token:{token}@github.com/{github.settings.org}/{repo_name}.git"

    await run_command(["git", "clone", clone_url, "/tmp/repo"])
    # ... run copier
```

## 5. Testing

### 5.1 MockGitHubClient

```python
# tests/fixtures/mock_github.py

class MockGitHubClient:
    """In-memory GitHub mock for unit/integration tests."""

    def __init__(self):
        self.repos: dict[str, Repository] = {}
        self.files: dict[str, dict[str, FileContent]] = {}
        self.secrets: dict[str, dict[str, str]] = {}
        self.calls: list[tuple[str, dict]] = []  # For assertions

    async def create_repo(self, name: str, **kwargs) -> Repository:
        self.calls.append(("create_repo", {"name": name, **kwargs}))

        repo = Repository(
            id=len(self.repos) + 1,
            name=name,
            full_name=f"test-org/{name}",
            html_url=f"https://github.com/test-org/{name}",
            clone_url=f"https://github.com/test-org/{name}.git",
        )
        self.repos[name] = repo
        self.files[name] = {}
        return repo

    async def get_repo(self, name: str) -> Repository:
        self.calls.append(("get_repo", {"name": name}))
        if name not in self.repos:
            raise NotFoundError(f"Repo {name} not found")
        return self.repos[name]

    async def delete_repo(self, name: str) -> None:
        self.calls.append(("delete_repo", {"name": name}))
        del self.repos[name]
        del self.files[name]

    # ... other methods
```

### 5.2 Fixture

```python
# tests/fixtures/conftest.py

import pytest
from unittest.mock import patch

@pytest.fixture
def mock_github():
    """Replace GitHubAppClient with mock."""
    mock = MockGitHubClient()

    with patch("shared.clients.github.GitHubAppClient", return_value=mock):
        yield mock

    # Cleanup: mock is in-memory, nothing to do
```

### 5.3 E2E with Real GitHub

```python
# tests/e2e/test_project_lifecycle.py

import pytest
import uuid

@pytest.mark.e2e
async def test_create_and_delete_repo():
    """E2E: Create real repo in test org, then delete."""
    # Uses real GitHubAppClient with GITHUB_ORG=project-factory-test
    github = GitHubAppClient()

    repo_name = f"test-{uuid.uuid4().hex[:8]}"

    try:
        repo = await github.create_repo(repo_name, "E2E test repo")

        assert repo.name == repo_name
        assert "project-factory-test" in repo.full_name

        # Verify via API
        fetched = await github.get_repo(repo_name)
        assert fetched.id == repo.id

    finally:
        # Cleanup
        await github.delete_repo(repo_name)
```

## 6. Migration from Current Code

### 6.1 Changes Required

| File | Change |
|------|--------|
| `shared/clients/github.py` | Remove `get_first_org_installation()` auto-detect. Use `settings.org`. |
| `services/scaffolder/src/main.py` | Remove duplicate JWT logic. Use `GitHubAppClient`. |
| `services/*/` | Add `GITHUB_ORG` to all docker-compose envs. |
| `.env.example` | Add `GITHUB_ORG=project-factory`. |

### 6.2 Breaking Changes

- `GITHUB_ORG` becomes **required**. Services fail fast if not set.
- `get_first_org_installation()` removed or deprecated.

## 7. GitHub App Setup

### 7.1 App Installation

One GitHub App installed on both organizations:

| Organization | Purpose |
|--------------|---------|
| `project-factory` | Production projects |
| `project-factory-test` | E2E tests, CI |

### 7.2 Required Permissions

| Permission | Access | Why |
|------------|--------|-----|
| **Repository** | Read & Write | Create repos, manage files |
| **Secrets** | Read & Write | Set Actions secrets |
| **Contents** | Read & Write | Read/write files |
| **Metadata** | Read | List repos |

### 7.3 Secrets Management

| Secret | Where | Value |
|--------|-------|-------|
| `GITHUB_APP_ID` | `.env`, CI secrets | Same for all envs |
| `GITHUB_APP_PRIVATE_KEY` | File mount, CI secret | Same key, different mount paths |
| `GITHUB_ORG` | `.env`, CI secrets | `project-factory` or `project-factory-test` |
