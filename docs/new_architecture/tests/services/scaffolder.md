# Testing Strategy: Scaffolder Service

**Service:** `services/scaffolder`
**Focus:** Correct application of templates and Git initialization.

## 1. Philosophy

The Scaffolder is an execution agent: it takes instructions (template + modules) and produces side effects (files + git commits).
- **Unit Tests**: Verify input mapping (API -> Copier answers).
- **Integration Tests**: Verify *local* file generation (Copier works) but *mock* the remote transport (Git push).
- **E2E Tests (Nightly)**: Verify full cycle with real GitHub push.

## 2. Test Pyramid

| Level | Scope | Focus | Implementation |
|-------|-------|-------|----------------|
| **Unit** | `Logic` class | Mapping inputs to template answers | Pure Python |
| **Integration** | `ScaffolderService` | Copier (Real) + Git (Mock) | File generation, Error handling |
| **E2E** | Full Pipeline | Copier (Real) + Git (Real) | GitHub auth, actual push |

## 3. Detailed Plans

### 3.1 Unit Tests (Logic)

**Key Scenarios:**

*   **Template selection:**
    *   Verify `modules=["backend"]` produces `use_backend=true` in copier answers.
    *   Verify `modules=["telegram"]` produces `use_telegram=true`.
*   **Path resolution:**
    *   Verify temp directory generation logic.

### 3.2 Integration Tests (Mocked Transport)

These tests run on every PR. We want to ensure the **template is valid** and **copier runs**, but we don't want to spam GitHub.

**Setup:**
*   Start `scaffolder` service.
*   **Mock GitHub**: Use `MockGitHubClient` (via fixture) to handle `get_repo_token` calls.
*   **Mock Git**: A wrapper around `git` command or `GitPython` that:
    *   Allows `git init`, `git add`, `git commit`.
    *   Intercepts `git push` and just returns success (verifying the remote URL).
*   **Real Copier**: We run `copier` against the actual `service-template` (located in `../service-template` relative to test runner).

**Scenarios:**

*   **Scaffold Project (Happy Path):**
    1.  Publish `ScaffolderMessage` (modules=[backend, telegram]).
    2.  Wait for success (checking mocked Git push calls).
    3.  **Assertion (Critical):** Check that files exist in the temp directory (e.g., `app/main.py`, `bot/dispatcher.py`). This catches broken copier templates!
    4.  Verify `PATCH /projects/{id} status=SCAFFOLDED` called on Mock API.
    5.  Verify `ScaffolderResult` published to `scaffolder:results`.

*   **Template Error:**
    1.  Force a copier error (e.g., invalid answers).
    2.  Verify service publishes correct error log/status.

### 3.3 E2E Tests (Nightly)

These run in the nightly pipeline against `codegen-orchestrator-test` org.

**Scenarios:**
*   **Real Push:**
    1.  Full scaffolding flow.
    2.  Verify `git push` actually succeeds to GitHub.
    3.  Verify repo exists via GitHub API check.

## 4. Test Infrastructure

*   **Fixtures:**
    *   `temp_workspace`: Automatically cleaned up temp dir.
    *   `mock_git`: Pytest fixture to intercept subprocess calls to git.
    *   `template_path`: Path to local `service-template`.

*   **Special Considerations:**
    *   `copier` requires Git to be installed in the test container.
    *   Unit tests need access to `service-template` to validate answer schema (optional, but good).

## 5. Mocks

### 5.1 Mock Git Adapter

```python
class MockGit:
    def __init__(self):
        self.pushes = []

    def push(self, remote, branch):
        self.pushes.append((remote, branch))
        return "Success"
        
    # init, add, commit are pass-through to real local git (so copier works)
    # only remote operations are mocked
```

*Actually, it's better to use real local git for init/add/commit so we can check if files were committed.*
