# CI/CD Improvement Plan

This document outlines a comprehensive, iterative strategy to modernize the CI/CD pipeline, improve developer experience, and ensure production reliability for the Codegen Orchestrator.

## 📊 Current State Analysis

**Strengths:**
- Basic Makefile exists.
- Containers are used for development/testing.
- Initial GitHub Actions deployment workflow exists.

**Critical Gaps:**
1.  **No Pre-commit Hooks**: Code quality checks rely on manual execution.
2.  **Inefficient Testing**: "All-or-nothing" approach re-installs dependencies on every run; no separation of unit/integration tests.
3.  **Heavy Docker Images**: Single stage builds include unnecessary build tools and cache.
4.  **Minimal CI**: No automated testing on Pull Requests.

---

## 🗓️ Iterative Implementation Plan

### Phase 1: Foundation & Hygiene ("Quick Wins")
**Goal**: Enforce code quality standards automatically locally and in CI.

#### 1.1. Pre-commit Hooks
Create `.pre-commit-config.yaml` to run fast checks before every commit.
- **Tools**:
    - `ruff format` (auto-format)
    - `ruff check --fix` (fast linting)
    - `yaml-check` (validate configuration files)
    - `check-added-large-files`
- **Action**: Run `pre-commit install` in dev setup.

#### 1.2. GitHub Actions (CI)
Review infrastructure code on every Push and Pull Request.
- **Workflow**: `.github/workflows/ci.yml`
- **Jobs**:
    - `lint`: Run Ruff, MyPy.
    - `test-smoke`: Run a minimal set of tests to ensure basic health.

#### 1.3. Cleanups
- **.dockerignore**: Add extensive ignore lists (`__pycache__`, `.git`, `tests/` in prod images) to reduce build context and image size.

---

### Phase 2: Test Infrastructure Revolution
**Goal**: Make tests fast, reliable, and granular. Stop "rebuilding the world" for every test run.

#### 2.1. Restructure Test Directory
Move tests closer to their services or categorize them clearly.
```text
services/
  api/
    tests/
      unit/          # Mocked external deps, fast
      integration/   # Requires DB/Redis
  langgraph/
    tests/
      unit/
      integration/
tests/
  e2e/               # Full system end-to-end tests
```

#### 2.2. Dedicated Test Dockerfiles
Create lightweight environments with pre-installed dependencies for testing.
- **Caching**: Install `test` dependencies in a layer that rarely changes.
- **Structure**:
    ```dockerfile
    FROM python:3.12-slim as base
    # ... install system deps ...
    COPY pyproject.toml .
    RUN pip install -e .[dev]  # <--- Cached layer
    COPY src ./src
    COPY tests ./tests
    ```

#### 2.3. Updated Makefile
Add granular commands for targeted testing.
- `make test-api-unit`
- `make test-langgraph-integration`
- `make test-all`

---

### Phase 3: Docker & Build Optimization
**Goal**: Faster builds and smaller production images.

#### 3.1. Multi-Stage Builds
Refactor all `Dockerfile`s to use build stages.
- **Builder Stage**: Install compilers, git, download dependencies.
- **Production Stage**: Copy only necessary artifacts/installed packages. Remove source code meant for testing/tooling.

#### 3.2. BuildKit & Caching
enable advanced caching in CI/CD.
- Use `DOCKER_BUILDKIT=1`.
- Leverage GitHub Actions Cache for Docker layers (`docker/build-push-action` with `gha` cache).

#### 3.3. Differential Testing (Smart CI)
Only run tests for services that changed.
- Use GitHub Actions `paths` filter or special actions (e.g., `tj-actions/changed-files`) to detect changes in `services/api/**` and trigger only API tests.

---

### Phase 4: Advanced CI/CD & Production Readiness
**Goal**: Security, compliance, and deployment automation.

#### 4.1. Security Scanning
- **Dependency Scan**: Check for CVEs in Python packages (e.g., `safety`, `pip-audit`).
- **Image Scan**: Scan Docker images for OS vulnerabilities (e.g., `trivy`).
- **Secret Scan**: Prevent API keys from leaking into git.

#### 4.2. Staging Environment
- Create `docker-compose.staging.yml`.
- Automate deployment to a Staging server on merge to `develop` branch.

#### 4.3. Observability in CI
- Upload test reports (JUnit XML) and coverage reports (Codecov) to track metrics over time.
- Slack/Telegram notifications on CI failure.

## 📝 Next Suggested Actions
Start with **Phase 1**:
1. Create `.pre-commit-config.yaml`.
2. Create `.github/workflows/ci.yml`.
3. Update `.dockerignore`.
