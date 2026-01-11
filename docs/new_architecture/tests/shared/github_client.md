# Testing Strategy: Shared GitHub Client

**Component:** `shared/clients/github.py`
**Focus:** Reliability of the central integration point with GitHub API.

## 1. Philosophy: Centralized Mocking

Since almost every service (Scheduler, Scaffolder, LangGraph) interacts with GitHub, we **centralize** the testing logic here to avoid scattered `respx` mocks and JSON files throughout the codebase.

We provide two things:
1.  **For the Client itself:** A robust suite of E2E `project-factory-test` tests and HTTP-level unit tests.
2.  **For Consumers (other services):** A reusable **`MockGitHubClient`** class (State-Based Fake) that behaves like the real thing but runs in-memory.

## 2. Test Pyramid

| Level | Scope | Focus | Implementation |
|-------|-------|-------|----------------|
| **Unit** | `GitHubAppClient` | Auth Logic, Token Cache, Error Handling | `pytest`, `respx` (for API errors) |
| **E2E** | `GitHubAppClient` | Real API Integration, breaking changes check | `pytest`, Real GitHub Test Org |
| **Fake** | `MockGitHubClient` | Providing a stable double for *other* services | In-Memory Dicts (`self.repos`) |

## 3. The Reusable `MockGitHubClient`

This is the **primary export** for other services' tests. It replaces the need for `respx` in Scheduler/Scaffolder.

### 3.1 Design
The Mock Client implements the same interface as `GitHubAppClient` but stores state in python dictionaries.

```python
# shared/tests/mocks/github.py

class MockGitHubClient:
    def __init__(self):
        # State
        self.repos: dict[str, Repository] = {}       # name -> Repository
        self.files: dict[str, dict[str, str]] = {}   # repo_name -> { path -> content }
        self.secrets: dict[str, dict[str, str]] = {} # repo_name -> { key -> value }
        
        # Behavior Configuration
        self.should_fail: bool = False
        self.fail_exception: Exception | None = None

    async def create_repo(self, name: str, description: str = "", private: bool = True) -> Repository:
        if self.should_fail:
            raise self.fail_exception or Exception("Simulated Failure")
            
        if name in self.repos:
            raise Exception(f"Repo {name} already exists") # Should raise specific GitHubError
            
        repo = Repository(
            id=len(self.repos) + 1,
            name=name,
            full_name=f"mock-org/{name}",
            html_url=f"https://github.com/mock-org/{name}",
            clone_url=f"https://github.com/mock-org/{name}.git",
            default_branch="main"
        )
        self.repos[name] = repo
        self.files[name] = {}
        return repo

    async def get_file(self, repo_name: str, path: str, ref: str = "main") -> FileContent:
        # Verify repo exists
        if repo_name not in self.repos:
            raise NotFoundError()
            
        # Verify file exists
        if path not in self.files[repo_name]:
            raise NotFoundError()
            
        return FileContent(
            path=path,
            content=self.files[repo_name][path],
            sha="mock-sha"
        )

    # ... implement all other methods (set_secret, list_repos, etc.)
```

### 3.2 Fixture Usage in Services
Services (e.g., Scheduler) will use a fixture that automatically patches the real client with this fake.

```python
# scheduler/tests/conftest.py
@pytest.fixture
def mock_github():
    fake = MockGitHubClient()
    # Pre-seed data if needed
    fake.repos["existing-repo"] = Repository(...)
    
    with patch("shared.clients.github.GitHubAppClient", return_value=fake):
        yield fake
```

## 4. Testing the Client Itself

We must ensure the *Real* client works, otherwise our *Fake* is lying.

### 4.1 Unit Tests (Protocol & Error Handling)
Use `respx` to mock the HTTP layer underneath `httpx` to verify edge cases the Fake doesn't cover.

**Scenarios:**
*   **Token Caching:**
    *   Mock `POST /app/installations/.../access_tokens`.
    *   Call `get_token()` twice.
    *   Assert only 1 HTTP call made (cache hit).
    *   Travel time forward > 1 hour.
    *   Call `get_token()`.
    *   Assert new HTTP call made.
*   **Error Mapping:**
    *   Mock `GET /repos/x` -> 404, 403, 429.
    *   Assert Client raises typed exceptions (`RepoNotFoundError`, `PermissionError`, `RateLimitError`).

### 4.2 E2E Tests (Nightly Verification)
To be run on a schedule or explicitly via `pytest -m e2e`.
**Requires:** `GITHUB_ORG=project-factory-test` and valid keys in environment.

**Scenario: Full Repo Lifecycle**
1.  **Setup:** Generate unique repo name `e2e-test-{uuid}`.
2.  **Create:** `client.create_repo(name)`. Assert object returned matches.
3.  **Verify:** Call `client.get_repo(name)`. Assert it exists on real GitHub.
4.  **File Ops:** `client.create_or_update_file(..., "README.md", "Hello")`.
5.  **Read:** `client.get_file(..., "README.md")` == "Hello".
6.  **Secrets:** `client.set_secret(..., "TEST_KEY", "123")`. (Client side verify only, can't read back secrets).
7.  **Teardown:** `client.delete_repo(name)`.
8.  **Verify Deleter:** `client.get_repo(name)` raises NotFound.

## 5. Implementation Plan

1.  **Package Structure:**
    ```
    shared/
    ├── clients/
    │   └── github.py
    └── tests/
        ├── conftest.py          # Fixtures
        ├── integration/         # Unit logic/Respx tests
        │   └── test_github_client.py
        ├── e2e/                 # Real API tests
        │   └── test_real_github.py
        └── mocks/               # THE EXPORTED FAKE
            └── github.py        # MockGitHubClient class
    ```

2.  **Migration Actions:**
    *   Create `MockGitHubClient`.
    *   Update `scheduler` tests to use it.
    *   Update `scaffolder` tests to use it.
